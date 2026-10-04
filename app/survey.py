"""What the album registry would find if it were filled in from the library.

Reads and reports. It opens no database, writes no file and changes no tag -
running it twice does nothing twice.

The registry is empty for music that was filed before it existed, which is
all 6,495 of Alex's tracks. That matters more than it sounds: the first
download of an album already in the library misses the table, mints a fresh
album UUID, and Navidrome shows the record twice. Filling the table in from
what the files already carry fixes that for every album at once.

For most albums the fill is a transcription - every file agrees about its
album UUID, so the row records it and no tag is touched. Two cases are not
transcribable, and this exists to count them before anything is written:

  * **split** - one album whose files carry more than one UUID. The old
    stamper picked the majority among a file's neighbours, so nine arriving
    tracks could outvote the one already filed. There is no single value to
    record, and choosing one leaves the rest wrong until something rewrites
    them.

  * **fused** - one UUID carried by files that belong to different albums.
    `tools/ensure_uuid.py` assigned per *directory*, so a flat dump of loose
    tracks came out as one album; the note in this project's history puts it
    at 101 albums sharing a UUID. Recording that verbatim would give every
    one of those albums the same identity and Navidrome would show them as a
    single enormous record.

Both need a person to choose, and the choice costs something real - a fresh
UUID for an album is a fresh Navidrome identity, which loses that album's
stars and play counts. So: count first, decide second, write third.

Grouped by the key `filer` and `registry` actually compute, not by directory
- `diskaudit` already answers the directory question, and the two differ
wherever a folder name and the tags inside it disagree.
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import cli, filer, registry, uuidtags, walk, workspace

log = logging.getLogger("navidrome_companion.survey")

# How many examples to carry per category. Enough to go and look at one;
# not so many that the report is the problem.
EXAMPLES = 5


@dataclass
class Album:
    """One album as the registry would key it, and what its files carry."""

    key: str
    artist: str
    album: str
    files: list[Path] = field(default_factory=list)
    uuids: collections.Counter = field(default_factory=collections.Counter)

    @property
    def stamped(self) -> int:
        return sum(self.uuids.values())

    @property
    def name(self) -> str:
        return f"{self.artist} - {self.album}"


@dataclass
class Survey:
    root: Path = Path()
    files: int = 0
    unreadable: list[str] = field(default_factory=list)
    untaggable: int = 0
    loose: list[str] = field(default_factory=list)

    ready: list[Album] = field(default_factory=list)
    unstamped: list[Album] = field(default_factory=list)
    partial: list[Album] = field(default_factory=list)
    split: list[Album] = field(default_factory=list)
    fused: list[list[Album]] = field(default_factory=list)

    @property
    def albums(self) -> int:
        return (len(self.ready) + len(self.unstamped) + len(self.partial)
                + len(self.split) + sum(len(g) for g in self.fused))

    def as_dict(self) -> dict[str, Any]:
        def rows(albums, limit=EXAMPLES):
            return [{"album": a.name, "files": len(a.files),
                     "uuids": len(a.uuids)} for a in albums[:limit]]

        return {
            "root": str(self.root),
            "files": self.files,
            "albums": self.albums,
            "untaggable": self.untaggable,
            "unreadable": len(self.unreadable),
            "loose": len(self.loose),
            "counts": {
                "ready": len(self.ready),
                "unstamped": len(self.unstamped),
                "partial": len(self.partial),
                "split": len(self.split),
                "fused": sum(len(group) for group in self.fused),
                "fused_groups": len(self.fused),
            },
            "examples": {
                "unstamped": rows(self.unstamped),
                "partial": rows(self.partial),
                "split": rows(self.split),
                "fused": [rows(group, 3) for group in self.fused[:3]],
                "unreadable": self.unreadable[:EXAMPLES],
                "loose": self.loose[:EXAMPLES],
            },
        }


def collect(root: Path) -> Survey:
    """Walk a library and group every file by the key the registry would use."""
    survey = Survey(root=root)
    if not root.is_dir():
        return survey

    albums: dict[str, Album] = {}
    for path in walk.library_files(root):
        if not path.is_file() or not uuidtags.is_audio(path):
            continue
        relative = str(path.relative_to(root))

        if not uuidtags.can_carry_tags(path):
            # A .wav has nowhere to put the tag, so it is outside this
            # question entirely rather than a problem to report.
            survey.untaggable += 1
            continue

        survey.files += 1
        try:
            _, album_uuid = uuidtags.read(path)
        except uuidtags.UnreadableFile as exc:
            survey.unreadable.append(f"{relative}  ({exc})")
            continue

        # The filer's own reading of the tags, so these keys are the keys a
        # download would compute. Anything else and the numbers would be
        # about a different question.
        meta = filer.read_meta(path)
        if not meta.names_album:
            # No album tag: its own record, keyed on its own track UUID.
            # There is nothing to reconcile and nothing to decide.
            survey.loose.append(relative)
            continue

        key = registry.album_key(meta.albumartist, meta.album)
        album = albums.get(key)
        if album is None:
            album = albums[key] = Album(key, meta.albumartist, meta.album)
        album.files.append(path)
        if album_uuid:
            album.uuids[album_uuid] += 1

    _sort(survey, albums)
    return survey


def _sort(survey: Survey, albums: dict[str, Album]) -> None:
    """Put every album in exactly one bucket, worst case winning."""
    # Which albums each UUID turns up in. An album is only transcribable if
    # its UUID means it and nothing else - a value shared with another album
    # cannot be recorded for both without fusing them.
    owners: dict[str, set[str]] = collections.defaultdict(set)
    for album in albums.values():
        for value in album.uuids:
            owners[value].add(album.key)

    fused_keys: dict[str, set[str]] = {}
    for value, keys in owners.items():
        if len(keys) > 1:
            fused_keys[value] = keys

    grouped: set[str] = set()
    for _value, keys in sorted(fused_keys.items()):
        group = [albums[key] for key in sorted(keys)]
        survey.fused.append(group)
        grouped.update(keys)

    for key, album in sorted(albums.items()):
        if key in grouped:
            continue
        if not album.uuids:
            survey.unstamped.append(album)
        elif len(album.uuids) > 1:
            survey.split.append(album)
        elif album.stamped < len(album.files):
            # One agreed UUID, but not every file carries it. The fill is
            # still a transcription; the files without it want stamping.
            survey.partial.append(album)
        else:
            survey.ready.append(album)


def report(survey: Survey) -> str:
    """The survey as something to read, rather than something to parse."""
    data = survey.as_dict()
    counts = data["counts"]
    out = [
        f"Library: {survey.root}",
        f"  {survey.files} taggable files in {survey.albums} albums"
        f"  ({survey.untaggable} untaggable, {len(survey.loose)} with no "
        f"album tag)",
        "",
        "The registry can be filled in from the files, with no tag changed:",
        f"  {counts['ready']:>5}  albums agree about their album UUID",
        f"  {counts['partial']:>5}  agree, but some files carry no UUID yet",
        f"  {counts['unstamped']:>5}  carry none at all (a fresh UUID, "
        f"nothing to lose)",
        "",
        "These need a decision before anything is written:",
        f"  {counts['split']:>5}  albums whose files disagree about which "
        f"album they are on",
        f"  {counts['fused']:>5}  albums sharing a UUID with another album "
        f"({counts['fused_groups']} groups)",
    ]
    if survey.unreadable:
        out += ["", f"  {len(survey.unreadable)} files could not be read."]

    for label, albums in (("split", survey.split),
                          ("unstamped", survey.unstamped)):
        if not albums:
            continue
        out += ["", f"{label}, first {min(EXAMPLES, len(albums))}:"]
        out += [f"  {a.name}  ({len(a.files)} files, {len(a.uuids)} UUIDs)"
                for a in albums[:EXAMPLES]]

    if survey.fused:
        out += ["", f"fused, first {min(3, len(survey.fused))} groups:"]
        for group in survey.fused[:3]:
            out.append(f"  one UUID across {len(group)} albums:")
            out += [f"    {a.name}  ({len(a.files)} files)" for a in group[:5]]
            if len(group) > 5:
                out.append(f"    ... and {len(group) - 5} more")

    out += ["", "Nothing was written. This only reads."]
    return "\n".join(out)


def main() -> int:
    cli.not_as_root("survey")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path,
                        help="a library to survey (default: every workspace)")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output")
    args = parser.parse_args()

    roots: list[Path]
    if args.root:
        roots = [args.root]
    else:
        roots = [space.library_path for space in workspace.existing()]
        if not roots:
            print("!! no workspace on disk names a library. Sign in once, or "
                  "pass a path.", file=sys.stderr)
            return 1

    surveys = [collect(root) for root in roots]
    if args.json:
        print(json.dumps([s.as_dict() for s in surveys], indent=2))
    else:
        print("\n\n".join(report(s) for s in surveys))
    return 0


if __name__ == "__main__":
    sys.exit(main())
