"""Write down the album UUIDs the library already carries.

The registry is empty for everything filed before it existed, so the first
download of an album already in the library misses the table, mints a fresh
UUID, and Navidrome shows the record twice. This transcribes what is already
on disk into the table, and that is the whole of it.

**It opens no file for writing.** Not "tries not to" - there is no write path
here at all. The only thing it changes is rows in `state.db`, and only ever by
inserting one that was not there. It never updates a row, never deletes one,
and never touches a tag, a filename or a track UUID.

That rules out two of the five kinds of album `survey` finds, and the ruling
out is the point:

  * **unstamped** - no file carries an album UUID. A row could be invented
    here, but then a later download of that album would carry the row's UUID
    while every file already on disk carries none, which is a way of
    splitting the record rather than a way of fixing it. Fixing it properly
    means writing the tag onto those files, which is a different pass with a
    different risk, so this one leaves them alone and says how many.

  * **split** and **fused** - the files contradict each other, or two albums
    claim one UUID. Both resolve by minting a fresh UUID for an album and
    rewriting its files, which loses that album's Navidrome identity and the
    stars and play counts hanging off it. That is a decision, and this does
    not make decisions.

What is left - the albums whose files agree - is a transcription, and the
large majority of any real library. `ready` albums all carry the value;
`partial` albums agree but have not written it onto every file yet, which
does not stop the row being right.

Idempotent by construction: a key that already has a row is left exactly as
it is, so running this twice does nothing the second time. Where the row and
the disk disagree, it says so and changes neither.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import registry, store, survey as survey_module, workspace
from .config import settings

log = logging.getLogger("navidrome_companion.backfill")


@dataclass
class Row:
    """One album UUID, and the key it is about to be recorded under."""

    key: str
    album_uuid: str
    name: str
    files: int


@dataclass
class Result:
    library_id: int = 0
    root: Path = Path()
    written: list[Row] = field(default_factory=list)
    already: list[Row] = field(default_factory=list)
    conflicts: list[tuple[Row, str]] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    applied: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "library_id": self.library_id,
            "root": str(self.root),
            "applied": self.applied,
            "written": len(self.written),
            "already_recorded": len(self.already),
            "conflicts": [
                {"album": row.name, "on_disk": row.album_uuid,
                 "in_registry": recorded}
                for row, recorded in self.conflicts
            ],
            "skipped": self.skipped,
        }


def plan(found: survey_module.Survey) -> list[Row]:
    """The rows a transcription would write, in a stable order.

    Only the albums whose files agree about their album UUID. `ready` carries
    it on every file and `partial` on some of them; either way there is one
    answer and the row records it.
    """
    rows = []
    for album in [*found.ready, *found.partial]:
        if len(album.uuids) != 1:
            # Defensive: `survey` already guarantees this, and a row written
            # from an album that disagrees with itself is the bug.
            continue
        (value, _count), = album.uuids.most_common(1)
        rows.append(Row(key=album.key, album_uuid=value, name=album.name,
                        files=len(album.files)))
    return sorted(rows, key=lambda row: row.name)


def run(library_id: int, root: Path, apply: bool = False) -> Result:
    """Survey a library and record what its files already agree on."""
    found = survey_module.collect(root)
    result = Result(library_id=library_id, root=root, applied=apply)
    result.skipped = {
        "unstamped": len(found.unstamped),
        "split": len(found.split),
        "fused": sum(len(group) for group in found.fused),
        "loose": len(found.loose),
        "unreadable": len(found.unreadable),
    }

    for row in plan(found):
        recorded = registry.known(library_id, row.key)
        if recorded == row.album_uuid:
            result.already.append(row)
            continue
        if recorded is not None:
            # The table and the disk disagree. Changing either one is a
            # decision about which is right, and this does not make those.
            result.conflicts.append((row, recorded))
            continue
        if apply:
            # `on_miss` is the whole mechanism: an existing row wins, so this
            # can only ever add one. Re-checked rather than assumed, because
            # another thread may have filed a track of this album since.
            settled = registry.uuid_for_key(library_id, row.key,
                                            on_miss=row.album_uuid)
            if settled != row.album_uuid:
                result.conflicts.append((row, settled))
                continue
        result.written.append(row)

    log.info("backfill %s: %d row(s) %s, %d already recorded, %d conflict(s)",
             root, len(result.written), "written" if apply else "to write",
             len(result.already), len(result.conflicts))
    return result


def report(result: Result) -> str:
    verb = "Recorded" if result.applied else "Would record"
    out = [
        f"Library {result.library_id}: {result.root}",
        f"  {verb} {len(result.written)} album UUID(s) the files already "
        f"agree on.",
        f"  {len(result.already)} were already recorded.",
    ]
    if result.conflicts:
        out += ["",
                f"  {len(result.conflicts)} album(s) where the registry and "
                f"the disk disagree. Neither was changed:"]
        out += [f"    {row.name}\n      on disk {row.album_uuid}\n"
                f"      in registry {recorded}"
                for row, recorded in result.conflicts[:5]]

    skipped = {k: v for k, v in result.skipped.items() if v}
    if skipped:
        out += ["", "  Left alone, because transcribing them is not possible:"]
        reasons = {
            "unstamped": "albums carrying no album UUID at all",
            "split": "albums whose files disagree about which album they are on",
            "fused": "albums sharing a UUID with another album",
            "loose": "files with no album tag",
            "unreadable": "files that could not be read",
        }
        out += [f"    {count:>5}  {reasons[name]}"
                for name, count in skipped.items()]
        out += ["", "  Run `python -m app.survey` to see which."]

    out += ["", "No file was opened for writing." if result.applied
            else "Nothing was written. Pass --apply to record these."]
    return "\n".join(out)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write the rows (default: say what would be)")
    parser.add_argument("--user", help="only this person's libraries")
    args = parser.parse_args()

    spaces = workspace.existing()
    if args.user:
        spaces = [space for space in spaces if space.username == args.user]
    if not spaces:
        print("!! no workspace on disk names a library. Sign in once first.",
              file=sys.stderr)
        return 1

    store.connect(settings.state_db)
    results = [run(space.library_id, space.library_path, args.apply)
               for space in spaces]
    print("\n\n".join(report(result) for result in results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
