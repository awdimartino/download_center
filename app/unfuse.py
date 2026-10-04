"""Give every album its own identity back.

`backfill.py` records the albums whose files already agree about their album
UUID. This is the other half: the ones that do not, which it deliberately
refuses to touch because writing a row for them would make the guess
permanent.

Two shapes of wrong, both from before the registry existed:

  * **fused** - several albums carrying one album UUID. Navidrome's
    `PID.Album` resolves `navidrome_album_uuid` first, so they collapse into
    a single record: five Kosu. albums became one row called "thirds.
    (VIP)", holding five files that belong to five different records.
  * **split** - one album whose files carry more than one UUID, so a record
    appears twice with some of its tracks in each.

And the same defect among files with no album tag at all: each is its own
record keyed on its own track UUID, so two of them sharing an album UUID
fuses two unrelated singles.

**What this costs, and what it cannot cost.** Navidrome's `PID.Track`
resolves `navidrome_uuid` first, so a track's identity - and every play
count, star and rating hanging off it - does not move when its album's UUID
changes. `check` refuses to run if any affected file lacks a track UUID,
because that is the assumption the whole operation rests on. What does move
is album-level annotation, which is why `Plan.annotations` reports it before
anything is written rather than after.

Nothing here moves a file. It writes one tag, in place, and records the old
value so the plan can be applied backwards.
"""

from __future__ import annotations

import collections
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import cli, registry, survey as survey_module, uuidtags

log = logging.getLogger("navidrome_companion.unfuse")


@dataclass
class Change:
    """One file's album UUID, and what it is about to become."""

    path: str            # relative to the library root, for a portable plan
    album: str           # what to call it in a report
    key: str             # the registry key this album will be recorded under
    was: str             # the UUID on the file now, "" if it carries none
    becomes: str
    why: str             # fused | split | loose

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "album": self.album, "key": self.key,
                "was": self.was, "becomes": self.becomes, "why": self.why}


@dataclass
class Plan:
    root: Path = Path()
    library_id: int = 0
    changes: list[Change] = field(default_factory=list)
    # Albums that keep the UUID they have, so a report can say what was left
    # alone as well as what moved.
    keepers: list[tuple[str, str, str]] = field(default_factory=list)
    # Every reason this must not be applied. Non-empty means refuse.
    blocked: list[str] = field(default_factory=list)
    # Album UUIDs that will stop existing, for the caller to weigh.
    retired: list[str] = field(default_factory=list)

    @property
    def files(self) -> int:
        return len(self.changes)

    @property
    def albums(self) -> int:
        return len({c.key for c in self.changes})

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "library_id": self.library_id,
            "files": self.files,
            "albums": self.albums,
            "keepers": [{"album": name, "key": key, "uuid": value}
                        for name, key, value in self.keepers],
            "retired": self.retired,
            "blocked": self.blocked,
            "changes": [c.as_dict() for c in self.changes],
        }


def _album_rows(connection: sqlite3.Connection, root: Path,
                paths: list[Path]) -> collections.Counter:
    """Which Navidrome album each of these files currently sits in.

    Used only to choose which member of a fused group keeps the shared
    UUID. The one Navidrome already shows under that identity is the least
    surprising keeper: its record stays where it is, and only the albums
    that were wrongly absorbed into it get new ones.
    """
    names: collections.Counter = collections.Counter()
    for path in paths:
        try:
            relative = str(path.relative_to(root)).replace("\\", "/")
        except ValueError:
            continue
        row = connection.execute(
            "select al.name from media_file mf"
            "  left join album al on al.id = mf.album_id"
            " where mf.path = ?", (relative,)).fetchone()
        if row and row[0]:
            names[row[0]] += 1
    return names


def _keeper(group: list[survey_module.Album],
            showing: collections.Counter) -> survey_module.Album:
    """Which album of a fused group keeps the UUID they share.

    The one Navidrome is already showing under it, where that is one of
    them. Otherwise the one with the most files, and alphabetically as the
    final tie-break so that two runs over the same library agree.
    """
    if showing:
        wanted = showing.most_common(1)[0][0]
        for album in group:
            if album.album == wanted:
                return album
    return sorted(group, key=lambda a: (-len(a.files), a.key))[0]


def _shared(group: list[survey_module.Album]) -> str:
    """The UUID that fused this group: the one more than one of them carry."""
    seen: collections.Counter = collections.Counter()
    for album in group:
        seen.update(set(album.uuids))
    for value, count in sorted(seen.items()):
        if count > 1:
            return value
    return ""


def _majority(album: survey_module.Album) -> str:
    """The UUID most of an album's files carry.

    Ties go to the alphabetically first, so that two runs over the same
    library agree - the order files are walked in must not decide this.
    """
    return min(album.uuids, key=lambda v: (-album.uuids[v], v))


def _fresh(taken: set[str]) -> str:
    """A UUID no album in this library is using."""
    while True:
        value = str(uuid.uuid4())
        if value not in taken:
            taken.add(value)
            return value


def build(found: survey_module.Survey, library_id: int,
          connection: sqlite3.Connection | None = None) -> Plan:
    """Decide what every ambiguous album's UUID should be. Writes nothing."""
    plan = Plan(root=found.root, library_id=library_id)

    # Every UUID anywhere in this library, so a minted one cannot collide
    # with an album this pass is not even looking at.
    taken: set[str] = set()
    for album in [*found.ready, *found.partial, *found.split,
                  *[a for g in found.fused for a in g]]:
        taken.update(album.uuids)

    # --- fused: one UUID, several albums ---------------------------------
    # An album can appear in two groups, if it shares one UUID with one
    # album and another with a second. Whichever group is reached first
    # decides it; planning it twice would write two answers to one file.
    settled: set[str] = set()

    for group in found.fused:
        group = [album for album in group if album.key not in settled]
        if len(group) < 2:
            # An earlier group settled the others, so nothing here shares a
            # UUID any more. What is left can still disagree with *itself*
            # though, and skipping it outright left one album split behind:
            # Radiohead's KID A MNESIA, whose partner in this group was Kid
            # A, already settled by the group it shared a different UUID
            # with. The survey files a split-and-fused album under fused, so
            # the split pass below never sees it either.
            for album in group:
                _unify(plan, album, found.root, "split")
                settled.add(album.key)
            continue
        showing: collections.Counter = collections.Counter()
        if connection is not None:
            showing = _album_rows(
                connection, found.root,
                [p for album in group for p in album.files])
        keeper = _keeper(group, showing)
        # The value that fused them, which is the one the keeper should end
        # up alone with - not merely its own majority, which for an album
        # that is fused *and* split may be the other one.
        shared = _shared(group)
        keeps = shared if shared in keeper.uuids else _majority(keeper)

        # The keeper may itself be split, and being chosen as keeper is not
        # a reason to leave it that way.
        for path in keeper.files:
            was = _read_album_uuid(path)
            if was != keeps:
                plan.changes.append(Change(
                    path=_relative(path, found.root), album=keeper.name,
                    key=keeper.key, was=was, becomes=keeps, why="fused"))
        plan.keepers.append((keeper.name, keeper.key, keeps))
        settled.add(keeper.key)

        for album in group:
            if album.key == keeper.key:
                continue
            value = _fresh(taken)
            for path in album.files:
                was = _read_album_uuid(path)
                plan.changes.append(Change(
                    path=_relative(path, found.root), album=album.name,
                    key=album.key, was=was, becomes=value, why="fused"))
            plan.retired.extend(v for v in album.uuids if v != keeps)
            settled.add(album.key)

    # --- split: one album, several UUIDs ---------------------------------
    for album in found.split:
        if album.key in settled:
            continue
        _unify(plan, album, found.root, "split")
        settled.add(album.key)

    # --- loose files sharing one identity --------------------------------
    by_uuid: dict[str, list[str]] = collections.defaultdict(list)
    for relative in found.loose:
        path = found.root / relative
        value = _read_album_uuid(path)
        if value:
            by_uuid[value].append(relative)
    for value, paths in sorted(by_uuid.items()):
        if len(paths) < 2 and value not in taken:
            continue
        # Each keeps its own record. There is no keeper to choose: a file
        # with no album tag is an album of one, and any two of them sharing
        # an identity is always wrong.
        for relative in sorted(paths):
            path = found.root / relative
            track_uuid, _ = _read_both(path)
            if not track_uuid:
                continue
            plan.changes.append(Change(
                path=relative, album=relative, key=registry.loose_key(track_uuid),
                was=value, becomes=_fresh(taken), why="loose"))

    plan.changes.sort(key=lambda c: (c.why, c.album, c.path))
    return plan


def _unify(plan: Plan, album: survey_module.Album, root: Path,
           why: str) -> str:
    """Put every file of one album onto the one UUID most of them carry."""
    best = _majority(album)
    for path in album.files:
        was = _read_album_uuid(path)
        if was == best:
            continue
        plan.changes.append(Change(
            path=_relative(path, root), album=album.name, key=album.key,
            was=was, becomes=best, why=why))
    plan.retired.extend(v for v in album.uuids if v != best)
    return best


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path)


def _read_album_uuid(path: Path) -> str:
    try:
        _, album_uuid = uuidtags.read(path)
    except Exception:
        return ""
    return album_uuid or ""


def _read_both(path: Path) -> tuple[str, str]:
    try:
        track_uuid, album_uuid = uuidtags.read(path)
    except Exception:
        return "", ""
    return track_uuid or "", album_uuid or ""


def check(plan: Plan) -> Plan:
    """Everything that must be true before a single tag is written.

    Fills `plan.blocked`. `apply` refuses while it is non-empty, because
    every entry is a way this could silently lose history rather than
    merely fail.
    """
    plan.blocked = []

    missing = []
    for change in plan.changes:
        path = plan.root / change.path
        if not path.exists():
            plan.blocked.append(f"{change.path}: gone since the survey")
            continue
        track_uuid, _ = _read_both(path)
        if not track_uuid:
            missing.append(change.path)
    if missing:
        # The assumption the whole operation rests on: PID.Track resolves
        # navidrome_uuid first, so a track's plays survive its album's UUID
        # changing. A file without one falls through to the albumid-based
        # fallback, and changing the album would change the track's identity
        # too - losing its history for real.
        plan.blocked.append(
            f"{len(missing)} file(s) carry no track UUID, so their play "
            f"history would not survive this: {', '.join(missing[:3])}")

    # Two changes for one file, or a minted UUID colliding with one already
    # in use, would both be silent.
    seen: dict[str, str] = {}
    for change in plan.changes:
        if change.path in seen and seen[change.path] != change.becomes:
            plan.blocked.append(
                f"{change.path}: planned twice, with different answers")
        seen[change.path] = change.becomes

    minted = [c.becomes for c in plan.changes]
    if len(set(minted)) != len({(c.key, c.becomes) for c in plan.changes}):
        plan.blocked.append("a UUID is planned for two different albums")

    return plan


@dataclass
class Outcome:
    written: int = 0
    failed: list[str] = field(default_factory=list)
    rows: int = 0
    applied: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"applied": self.applied, "files_written": self.written,
                "registry_rows": self.rows, "failed": self.failed}


def apply_plan(plan: Plan, dry_run: bool = True) -> Outcome:
    """Write the plan, tags first and the registry after.

    Tags first because they are the thing Navidrome reads; a registry row
    pointing at a UUID no file carries would be a lie the next download
    acts on. If a write fails the file is reported and the run continues -
    the others are still correct, and a half-applied plan is re-runnable
    because a file already carrying the right UUID is skipped.
    """
    outcome = Outcome(applied=not dry_run)
    if plan.blocked:
        raise ValueError("refusing to apply a blocked plan: "
                         + "; ".join(plan.blocked))

    for change in plan.changes:
        path = plan.root / change.path
        if dry_run:
            outcome.written += 1
            continue
        try:
            uuidtags.write(path, None, change.becomes)
            _, wrote = uuidtags.read(path)
            if wrote != change.becomes:
                raise RuntimeError(f"read back {wrote!r}")
            outcome.written += 1
        except Exception as exc:
            outcome.failed.append(
                f"{change.path}: {type(exc).__name__}: {exc}")

    # One row per album, after every file of it is carrying the value.
    # Keepers included: these albums had no registry row at all - the
    # survey will not record an ambiguous one - so freeing the others and
    # leaving the keeper unrecorded would fix only half of it.
    failures = "".join(outcome.failed)
    done = {c.path for c in plan.changes if f"{c.path}: " not in failures}
    rows = {(name_key, value) for _n, name_key, value in plan.keepers}
    for change in plan.changes:
        if change.path in done:
            rows.add((change.key, change.becomes))
    for key, value in sorted(rows):
        if not dry_run:
            registry.reassign(plan.library_id, key, value)
        outcome.rows += 1

    log.info("unfuse %s: %d file(s) %s, %d registry row(s), %d failure(s)",
             plan.root, outcome.written,
             "written" if not dry_run else "to write",
             outcome.rows, len(outcome.failed))
    return outcome


def save(plan: Plan, path: Path) -> Path:
    """The plan as JSON, which is what makes this reversible.

    Every change records the UUID the file carried before it, so undoing is
    the same write with `was` and `becomes` swapped.
    """
    path.write_text(json.dumps(plan.as_dict(), indent=2), encoding="utf-8")
    return path


def report(plan: Plan) -> str:
    counts: collections.Counter = collections.Counter(
        c.why for c in plan.changes)
    out = [
        f"Library: {plan.root}",
        f"  {plan.files} file(s) across {plan.albums} album(s) would be "
        f"re-stamped",
        f"    fused albums given their own identity : {counts['fused']}",
        f"    split albums brought back together    : {counts['split']}",
        f"    loose files sharing one identity      : {counts['loose']}",
        f"  {len(plan.keepers)} album(s) keep the UUID they have",
        f"  {len(set(plan.retired))} UUID(s) stop being used",
    ]
    if plan.blocked:
        out.append("  REFUSED:")
        out.extend(f"    {reason}" for reason in plan.blocked)
    return "\n".join(out)


# --- the command ------------------------------------------------------------

def run(library_id: int, root: Path, apply: bool = False,
        plan_file: Path | None = None) -> tuple[Plan, Outcome]:
    """Survey a library, decide, and optionally write."""
    from . import navidrome

    connection = None
    try:
        connection = navidrome.open_db()
    except Exception as exc:
        # Only used to pick which member of a fused group keeps the shared
        # UUID. Without it the choice falls back to file count, which is
        # deterministic but less considerate of what is already on screen.
        log.warning("cannot read Navidrome, choosing keepers by size: %s", exc)

    found = survey_module.collect(root)
    if connection is not None:
        with connection:
            plan = build(found, library_id, connection)
    else:
        plan = build(found, library_id)
    check(plan)

    if plan_file is not None and plan.changes:
        save(plan, plan_file)
    return plan, apply_plan(plan, dry_run=not apply)


def plan_path(base: Path, library_id: int) -> Path:
    """One plan file per library. A single --plan FILE was rewritten for
    each workspace in turn, so only the last library's plan - the thing
    that makes a run reversible - survived."""
    return base.with_name(f"{base.stem}-{library_id}{base.suffix}")


def main() -> int:
    cli.not_as_root("unfuse")
    import argparse
    import sys

    from . import store, workspace
    from .config import settings

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write the tags (default: say what would change)")
    parser.add_argument("--user", help="only this person's libraries")
    parser.add_argument("--plan", metavar="FILE", default=None,
                        help="save the plan as JSON - every file with the "
                             "UUID it carried, which is what makes this "
                             "reversible")
    args = parser.parse_args()

    spaces = workspace.existing()
    if args.user:
        spaces = [space for space in spaces if space.username == args.user]
    if not spaces:
        print("!! no workspace on disk names a library. Sign in once first.",
              file=sys.stderr)
        return 1

    store.connect(settings.state_db)
    failed = False
    for space in spaces:
        plan_file = (plan_path(Path(args.plan), space.library_id)
                     if args.plan else None)
        plan, outcome = run(space.library_id, space.library_path, args.apply,
                            plan_file)
        print(report(plan))
        if plan_file:
            print(f"  plan saved to {plan_file}")
        if plan.blocked:
            failed = True
            continue
        if not args.apply:
            print("  DRY RUN. Nothing written. Re-run with --apply.")
            continue
        print(f"  wrote {outcome.written} file(s), "
              f"{outcome.rows} registry row(s)")
        for line in outcome.failed[:10]:
            print(f"    !! {line}")
        if outcome.failed:
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
