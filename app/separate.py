"""Give each album in a shared folder a folder of its own.

Some folders hold more than one album: `Vulpey/Dormant` holding *Dormant*
and *Squirm*, `Wolfmother/Wolfmother 10TH Anniversary` holding a track of
*Wolfmother*. The Library treats a folder as one record, so every album
action refuses them (`filer.require_one_album`) - which is safe, and leaves
them stuck. Most likely `unfuse.py` gave such albums their own UUIDs without
moving their files, which it says it never does.

**What moves.** In each such folder the album whose own name is this
folder stays; if none is, none stays. Every other album's tracks are filed
where their tags say they belong,
through `filer.file_track` - the same code a download is filed with - so
they land in `Artist/Album/` beside any copies already there and join that
album's identity.

**What does not.** A folder holding one album under a name the filer would
not give it (`voljum/2022 - dayscapes`, from an older layout) is left
alone; it is not wrong, only named differently. So is a file with no album
tag: its place by the tags is an `Unknown Album` folder, which is a worse
home than the album folder it is in, and the fix for it is tagging. Two
albums whose names make the same folder (`AC/DC` and `AC_DC`) cannot be
separated by moving and are reported instead.

**What it cannot cost.** Navidrome identifies a track by its
`navidrome_uuid`, which a move does not touch, so plays, stars and ratings
go with every file. No folder cover goes with a moved track: the folder it
leaves belongs to the album that stays.

Run by hand, per person, with a dry run first:

    python -m app.separate --user alex            # what would move
    python -m app.separate --user alex --apply
"""

from __future__ import annotations

import collections
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import cli, filer, registry, uuidtags, walk, workspace

log = logging.getLogger("navidrome_companion.separate")


@dataclass
class Move:
    source: str          # relative to the library root
    target: str          # where its tags say it belongs, relative too
    album: str           # "Artist - Album", for the report


@dataclass
class Folder:
    folder: str          # relative to the library root
    keeps: str           # the album that stays
    moves: list[Move] = field(default_factory=list)


@dataclass
class Plan:
    library_id: int
    root: Path
    folders: list[Folder] = field(default_factory=list)
    # Folders that cannot be fixed by moving, with why.
    stuck: list[str] = field(default_factory=list)

    @property
    def files(self) -> int:
        return sum(len(f.moves) for f in self.folders)

    def as_dict(self) -> dict:
        return {"library_id": self.library_id, "root": str(self.root),
                "folders": [asdict(f) for f in self.folders],
                "stuck": self.stuck}


def _albums_in(files: list[Path]) -> tuple[dict[str, list[tuple[Path, filer.Meta]]],
                                            list[tuple[Path, filer.Meta]]]:
    """The files that name an album, grouped by its key, and those that
    do not."""
    named: dict[str, list[tuple[Path, filer.Meta]]] = collections.defaultdict(list)
    loose: list[tuple[Path, filer.Meta]] = []
    for path in files:
        try:
            meta = filer.read_meta(path)
        except Exception as exc:
            log.warning("cannot read %s: %s", path, exc)
            continue
        if meta.names_album:
            named[registry.album_key(meta.albumartist, meta.album)].append((path, meta))
        else:
            loose.append((path, meta))
    return named, loose


def build(space: workspace.Workspace) -> Plan:
    root = space.library_path
    plan = Plan(library_id=space.library_id, root=root)
    by_folder: dict[Path, list[Path]] = collections.defaultdict(list)
    for path in walk.library_files(root):
        # A dot-file is a half-written copy the filer has not renamed yet.
        if uuidtags.is_audio(path) and not path.name.startswith("."):
            by_folder[path.parent].append(path)

    for folder, files in sorted(by_folder.items()):
        albums, loose = _albums_in(files)

        def home(path: Path, meta: filer.Meta) -> Path:
            return filer.destination(space, meta, path.suffix.lower()).parent

        homes = {key: home(*albums[key][0]) for key in albums}
        # `Artist/Unknown Album` is where files with no album tag belong. A
        # tagged album in it is in the wrong place even when it is the only
        # one - `Club2Tokyo/Unknown Album` holding *Pink Summer* beside two
        # loose files - so the loose files claim the folder.
        loose_home = any(home(*one) == folder for one in loose)
        if len(albums) < 2 and not (albums and loose_home):
            continue

        here = [key for key in albums if homes[key] == folder]
        labels = {key: f"{files[0][1].albumartist} - {files[0][1].album}"
                  for key, files in albums.items()}
        # The album this folder is named for stays. When none is - `Kosu_/
        # Daft_` from an older sanitiser, holding *thirds. (VIP)* and four
        # others - every album goes home: keeping the biggest would leave
        # one record in a folder named for another.
        keeps = here[0] if here else None
        if keeps is None and loose_home:
            keeps = "loose"
            label_keeps = "the files with no album tag"
        else:
            label_keeps = labels[keeps] if keeps else "(none; every album moves)"

        colliding = here[1:]
        if colliding:
            plan.stuck.append(
                f"{folder.relative_to(root)}: {', '.join(labels[k] for k in colliding)} "
                f"would be filed into this same folder as {labels[keeps]}; their "
                "names make one folder, so they need renaming, not moving")

        entry = Folder(folder=str(folder.relative_to(root)), keeps=label_keeps)
        for key in sorted(albums):
            if key == keeps or key in colliding:
                continue
            for path, meta in albums[key]:
                target = filer.destination(space, meta, path.suffix.lower())
                entry.moves.append(Move(source=str(path.relative_to(root)),
                                        target=str(target.relative_to(root)),
                                        album=labels[key]))
        if entry.moves:
            plan.folders.append(entry)
    return plan


@dataclass
class Outcome:
    moved: int = 0
    failed: list[str] = field(default_factory=list)
    # Where each file actually went, which can differ from the plan by a
    # " (2)" when the name was taken.
    went: list[tuple[str, str]] = field(default_factory=list)


def apply_plan(space: workspace.Workspace, plan: Plan) -> Outcome:
    """Move every planned file, one at a time, and tidy what they left.

    A file that fails is reported and the rest go on: each move stands on
    its own, and a re-run plans only what is still in the wrong place.
    """
    outcome = Outcome()
    root = plan.root
    for entry in plan.folders:
        left = root / entry.folder
        went_to: set[Path] = set()
        for move in entry.moves:
            source = root / move.source
            try:
                if not source.is_file():
                    raise FileNotFoundError("no longer there")
                filed = filer.file_track(space, source, carry_cover=False)
                went_to.add(filed.path.parent)
                outcome.moved += 1
                outcome.went.append((move.source, str(filed.path.relative_to(root))))
            except Exception as exc:
                outcome.failed.append(f"{move.source}: {type(exc).__name__}: {exc}")
        # Prunes nothing while the album that stays is still in it; here
        # for the case where every album in a folder was moved elsewhere.
        filer.leave_folder(left, went_to, space.library_path)
    log.info("separate %s: %d moved, %d failed", root, outcome.moved,
             len(outcome.failed))
    return outcome


def report(plan: Plan, limit: int | None = None) -> str:
    out = [f"Library {plan.library_id} at {plan.root}",
           f"  {len(plan.folders)} folder(s) hold more than one album; "
           f"{plan.files} file(s) would move"]
    for entry in plan.folders[:limit]:
        out.append(f"  {entry.folder}")
        out.append(f"      stays: {entry.keeps}")
        by_album: dict[str, list[Move]] = collections.defaultdict(list)
        for move in entry.moves:
            by_album[move.album].append(move)
        for album, moves in by_album.items():
            out.append(f"      moves: {album} ({len(moves)}) -> "
                       f"{Path(moves[0].target).parent}")
    if limit is not None and len(plan.folders) > limit:
        out.append(f"  ... and {len(plan.folders) - limit} more")
    if plan.stuck:
        out.append("  CANNOT BE SEPARATED BY MOVING:")
        out.extend(f"    {line}" for line in plan.stuck)
    return "\n".join(out)


def main() -> int:
    cli.not_as_root("separate")
    import argparse
    import sys

    from . import navidrome, store
    from .config import settings

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="move the files (default: say what would move)")
    parser.add_argument("--user", help="only this person's libraries")
    parser.add_argument("--plan", metavar="FILE",
                        help="save the plan, and on --apply where each file "
                             "went, as JSON")
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
    moved = 0
    for space in spaces:
        plan = build(space)
        print(report(plan))
        saved = plan.as_dict()
        if args.apply and plan.folders:
            outcome = apply_plan(space, plan)
            moved += outcome.moved
            saved["went"] = outcome.went
            saved["failed"] = outcome.failed
            print(f"  moved {outcome.moved} file(s)")
            for line in outcome.failed[:20]:
                print(f"    !! {line}")
            failed = failed or bool(outcome.failed)
        elif not args.apply:
            print("  DRY RUN. Nothing moved. Re-run with --apply.")
        if args.plan:
            path = Path(args.plan)
            path = path.with_name(f"{path.stem}-{space.library_id}{path.suffix}")
            path.write_text(json.dumps(saved, indent=2), encoding="utf-8")
            print(f"  plan saved to {path}")
    if moved:
        # The files are where Navidrome will look; it has to be told.
        navidrome.notify()
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
