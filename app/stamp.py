"""Assigning identity tags after beets has filed a file.

Deliberately not done at download time. Staging is hidden from Navidrome, so
a file is first seen once it is already in the library with its final tags -
which means identity only has to be stable before anything gets *annotated*,
not before the file exists. That is a much weaker requirement, and waiting has
a real advantage: the album UUID cannot be chosen correctly until the file is
in its final directory, because that is the first moment its siblings are
visible.

Downloading track five of an album already in the library is the case that
matters. Given a fresh album UUID at ingestion it would arrive as a second,
one-track copy of that album. Assigned here, it inherits the UUID its new
siblings already agree on and simply joins the record.

Track UUIDs, once written, are never touched again - they carry the stars.
"""

from __future__ import annotations

import collections
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import uuidtags

log = logging.getLogger("download_center.stamp")


@dataclass
class Result:
    tracks_written: int = 0
    albums_written: int = 0
    already_stamped: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.tracks_written or self.albums_written)


def _album_key(path: Path, album: str) -> tuple:
    """What counts as one album on disk.

    A directory is usually one album, but not always: the singles destination
    collects unrelated tracks from many artists under one folder, and grouping
    by directory alone would fuse them into a single fictional album. Files
    that agree on an album tag are one album; a file with no album tag is a
    loose single and stands alone.
    """
    return (path.parent, album) if album else (path.parent, path.name)


def _choose_album_uuid(existing: list[tuple[Path, str]]) -> str:
    """Pick the UUID a group should settle on.

    The value most files already carry wins, so the fewest files are touched.
    Ties go to the oldest file: at one existing track and one arrival, a
    straight count is a coin flip that could rename an album Navidrome
    already knows, and an established record should always beat a newcomer.
    """
    counts = collections.Counter(value for _, value in existing)
    best = max(counts.values())
    contenders = {value for value, n in counts.items() if n == best}
    if len(contenders) == 1:
        return contenders.pop()

    oldest = min(
        (pair for pair in existing if pair[1] in contenders),
        key=lambda pair: pair[0].stat().st_mtime,
    )
    return oldest[1]


def _stampable(path: Path) -> bool:
    return (path.is_file() and uuidtags.is_audio(path)
            and uuidtags.can_carry_tags(path))


def stamp(paths: list[Path]) -> Result:
    """Ensure every audio file under `paths` carries both identity tags.

    Files already in the library are read too, but never written. They are
    what a new arrival inherits its album UUID from, and reading the whole
    directory is also what keeps unrelated singles that happen to share a
    folder from being fused into one album.
    """
    result = Result()

    requested: set[Path] = set()
    for path in paths:
        if _stampable(path):
            requested.add(path)
        elif path.is_dir():
            requested.update(p for p in path.rglob("*") if _stampable(p))

    # Everything in the same directories, so grouping sees the full picture.
    considered = set(requested)
    for directory in {path.parent for path in requested}:
        considered.update(p for p in directory.iterdir() if _stampable(p))

    groups: dict[tuple, list[tuple[Path, str | None, str | None]]] = \
        collections.defaultdict(list)

    for path in sorted(considered):
        try:
            track_uuid, album_uuid = uuidtags.read(path)
        except uuidtags.UnreadableFile as exc:
            if path in requested:
                result.failures.append(f"{path.name}: {exc}")
            continue
        groups[_album_key(path, uuidtags.album_name(path))].append(
            (path, track_uuid, album_uuid))

    for members in groups.values():
        known = [(path, value) for path, _, value in members if value]
        album_uuid = _choose_album_uuid(known) if known else str(uuid.uuid4())

        for path, track_uuid, current_album in members:
            if path not in requested:
                continue           # already in the library; read, not written
            needs_track = track_uuid is None
            needs_album = current_album != album_uuid
            if not (needs_track or needs_album):
                result.already_stamped += 1
                continue
            try:
                _write(path,
                       str(uuid.uuid4()) if needs_track else None,
                       album_uuid if needs_album else None)
            except Exception as exc:
                result.failures.append(f"{path.name}: {type(exc).__name__}: {exc}")
                continue
            result.tracks_written += needs_track
            result.albums_written += needs_album

    if result.failures:
        log.warning("stamping had %d failure(s): %s",
                    len(result.failures), "; ".join(result.failures[:3]))
    return result


def spanning_albums(root: Path) -> dict[str, list[Path]]:
    """Album UUIDs that turn up in more than one directory.

    One directory is one album, so a UUID in two of them means Navidrome will
    present unrelated tracks as a single record. This happens when files are
    stamped together and filed apart afterwards - which is what the original
    backfill did, stamping in place before reorganising. The pipeline now
    stamps after beets files a track, so it should not recur, but anything
    that moves files without re-stamping would do it again.
    """
    directories: dict[str, set[Path]] = collections.defaultdict(set)
    for path in root.rglob("*"):
        if not _stampable(path):
            continue
        try:
            _, album_uuid = uuidtags.read(path)
        except uuidtags.UnreadableFile:
            continue
        if album_uuid:
            directories[album_uuid].add(path.parent)
    return {uuid_: sorted(dirs) for uuid_, dirs in directories.items()
            if len(dirs) > 1}


def resplit(root: Path, apply: bool = False) -> Result:
    """Give each directory its own album UUID where one has spread.

    The mirror of the merge the stamper does. One directory keeps the shared
    value - the one holding the most files, so the fewest are rewritten - and
    every other gets a fresh UUID. Track UUIDs are never touched, so nothing
    a star is attached to changes.
    """
    result = Result()

    for album_uuid, directories in spanning_albums(root).items():
        members = {
            directory: [p for p in directory.iterdir() if _stampable(p)]
            for directory in directories
        }
        # The largest directory keeps the existing value; ties go to the one
        # whose files are oldest, so an established album outranks an arrival.
        keeper = max(
            members,
            key=lambda d: (len(members[d]),
                           -min((p.stat().st_mtime for p in members[d]),
                                default=0)),
        )
        for directory, files in members.items():
            if directory == keeper:
                continue
            fresh = str(uuid.uuid4())
            for path in files:
                try:
                    _, current = uuidtags.read(path)
                except uuidtags.UnreadableFile as exc:
                    result.failures.append(f"{path.name}: {exc}")
                    continue
                if current != album_uuid:
                    continue          # already belongs to a different album
                if not apply:
                    result.albums_written += 1
                    continue
                try:
                    _write(path, None, fresh)
                    result.albums_written += 1
                except Exception as exc:
                    result.failures.append(
                        f"{path.name}: {type(exc).__name__}: {exc}")
    return result


def _write(path: Path, track_uuid: str | None, album_uuid: str | None) -> None:
    """Write and verify, preserving mtime.

    A write that did not survive the round trip is a failure however plausible
    it looked, so it is read back rather than assumed. mtime is restored
    because nothing here alters the audio - the file genuinely is the same
    recording it was a moment ago.
    """
    stat = path.stat()
    uuidtags.write(path, track_uuid, album_uuid)
    os.utime(path, (stat.st_atime, stat.st_mtime))

    verify_track, verify_album = uuidtags.read(path)
    if track_uuid is not None and verify_track != track_uuid:
        raise RuntimeError(f"track UUID did not round-trip (got {verify_track!r})")
    if album_uuid is not None and verify_album != album_uuid:
        raise RuntimeError(f"album UUID did not round-trip (got {verify_album!r})")
