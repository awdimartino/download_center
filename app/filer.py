"""Puts a finished file in the library, at a path it will never leave.

This replaced staging, which held music *outside* the library until beets
agreed to admit it - and beets rejected 82% of what it was given, almost all
of it for mechanical reasons that had nothing to do with the music. So the
gate is gone: a file is tagged, given its identity, and filed where it
belongs, immediately.

One function does it, and it does the same thing whether the file came from a
download job or was dropped into the inbox by hand. That is deliberate. The
album-versus-single routing, the completeness check and the regrouping pass
all existed because those two roads were different, and every one of them was
a place a track could end up somewhere nobody would look for it.

The path is computed from the file's own tags:

    $albumartist/$album/$disc-$track - $title.ext

and then frozen. No automatic process moves a file afterwards. Navidrome
identifies a track by the UUID written into it and groups albums by tag, never
by path, so a path that drifts away from the tags is cosmetic - it matters
only to a human browsing the filesystem. Only a retag confirmed in the review
page moves anything, and the UUID means nothing is lost when it does.
"""

from __future__ import annotations

import errno
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import registry, uuidtags, workspace

log = logging.getLogger("download_center.filer")

# Characters Windows forbids in a filename, plus control characters.
# The backslash is escaped: inside a character class `\|` escapes the pipe
# and the backslash itself never joins the set, so "AC\DC" came through
# untouched and became a directory separator.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_TRAILING = re.compile(r"[. ]+$")
# Device names Windows still reserves, with or without an extension.
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

MAX_COMPONENT = 110

UNKNOWN_ARTIST = "Unknown Artist"
UNKNOWN_ALBUM = "Unknown Album"

# A leading integer, however the tag spells it: "3", "3/9", "03".
_NUMBER = re.compile(r"\s*(\d+)")


def sanitize(name: str) -> str:
    """Make a string safe as a single path component on Windows and Linux."""
    cleaned = _ILLEGAL.sub("_", name or "").strip()
    cleaned = _TRAILING.sub("", cleaned)
    if len(cleaned) > MAX_COMPONENT:
        cleaned = cleaned[:MAX_COMPONENT].rstrip(" .")
    if cleaned.split(".")[0].lower() in _RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned or "unknown"


@dataclass(frozen=True)
class Meta:
    """What the path is built from. Every field has an answer."""

    albumartist: str
    album: str
    title: str
    track_no: int = 0
    disc_no: int = 0
    multi_disc: bool = False
    # Whether the file actually said which album it is on, as opposed to
    # having had `Unknown Album` filled in for it. The path does not care,
    # but identity does: a file that never named an album is not on one.
    names_album: bool = True


@dataclass(frozen=True)
class Filed:
    """Where a file ended up and what identity it carries."""

    path: Path
    track_uuid: str
    album_uuid: str
    album_key: str
    # False when the UUIDs could not be written. The file is still filed -
    # in the library and playable beats in scratch space and lost - but it
    # carries no identity, so no play count can follow it, and the caller is
    # the last thing in a position to say so.
    identified: bool = True


def _number(value) -> int:
    match = _NUMBER.match(str(value or ""))
    return int(match.group(1)) if match else 0


def _total(value) -> int:
    """The second half of a "3/9" tag, or 0 when there is only one half."""
    _, _, tail = str(value or "").partition("/")
    return _number(tail)


def read_meta(path: Path) -> Meta:
    """The tags the path is built from, with a real answer for each.

    The *album* artist, not the track artist. A guest credited on one track
    must not split an album in two - the same trap that made beets read a
    downloaded Thriller as a Various Artists compilation.

    A file whose tags cannot be read at all is still filed. It goes to
    `Unknown Artist/Unknown Album/` under its own filename and shows up in
    the review list, which is a far better place for it than the holding
    directory it used to sit in unseen.
    """
    tags = {}
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(path, easy=True)
        if audio is not None and audio.tags is not None:
            tags = audio.tags
    except Exception as exc:
        log.debug("could not read tags from %s: %s", path.name, exc)

    def first(key: str) -> str:
        try:
            values = tags.get(key)
        except Exception:
            return ""
        if not values:
            return ""
        value = values[0] if isinstance(values, (list, tuple)) else values
        return str(value).strip()

    named_album = first("album")
    album = named_album or UNKNOWN_ALBUM
    artist = (first("albumartist") or first("artist") or UNKNOWN_ARTIST)
    title = first("title") or path.stem

    disc = first("discnumber")
    return Meta(
        albumartist=artist,
        album=album,
        title=title,
        names_album=bool(named_album),
        track_no=_number(first("tracknumber")),
        disc_no=_number(disc),
        # Spotify does not report how many discs a release has, so a disc
        # number above one is the other half of the signal. A single-disc
        # album never carries one, and on a release that does, disc 1 losing
        # its prefix while disc 2 keeps one is cosmetic - the two cannot
        # collide, because the prefix is what differs.
        multi_disc=_total(disc) > 1 or _number(disc) > 1,
    )


def track_filename(meta: Meta, suffix: str) -> str:
    """`$disc-$track - $title.ext`, with each part omitted when unknown.

    An untracked file is named for its title alone rather than `00 - Title`.
    There are a lot of them - the whole point of this redesign is that they
    land in the library instead of waiting outside it - and a library where
    every hand-dropped file sorts first under a fake track zero reads as
    broken. Two of them with one title collide, and `_move_into_place`
    numbers the second rather than overwriting the first; the tagging page is
    where that gets fixed properly.
    """
    if not meta.track_no:
        return f"{sanitize(meta.title)}{suffix}"
    number = (f"{meta.disc_no or 1}-{meta.track_no:02d}" if meta.multi_disc
              else f"{meta.track_no:02d}")
    return f"{sanitize(f'{number} - {meta.title}')}{suffix}"


def destination(space: workspace.Workspace, meta: Meta, suffix: str) -> Path:
    """Where this file lives, from now on."""
    return (space.library_path / sanitize(meta.albumartist)
            / sanitize(meta.album) / track_filename(meta, suffix))


# --- retagging --------------------------------------------------------------
#
# The one thing that is allowed to move a file after it is written, because a
# person confirmed it. Everything else treats the path as frozen.

def album_key_of(folder: Path) -> str:
    """The registry key the files in a folder currently answer to.

    Read before a retag, so the album can be followed to whatever it becomes.
    Taken from the first file: they are one album, and after the filer put
    them there they agree about which.
    """
    files = sorted(p for p in folder.rglob("*")
                   if p.is_file() and uuidtags.is_audio(p))
    if not files:
        return ""
    meta = read_meta(files[0])
    if not meta.names_album:
        # Nothing to follow: a folder of files that do not name an album has
        # no album key to move, and each of them is its own record.
        return ""
    return registry.album_key(meta.albumartist, meta.album)


def after_retag(space: workspace.Workspace, paths: list[Path],
                old_key: str) -> str:
    """Follow a confirmed retag, and return the album UUID that settled.

    Ordinarily the album keeps the UUID it has and only its key moves, so its
    Navidrome identity survives and album-level stars and play counts survive
    with it. If the new key is already registered - the record is in the
    library, correctly tagged - the incumbent wins and these files are
    rewritten to join it, because only the newcomer can be rewritten.
    """
    live = [path for path in paths
            if path.is_file() and uuidtags.is_audio(path)]
    if not live:
        return ""

    meta = read_meta(live[0])
    if not meta.names_album:
        # A retag that did not give these files an album is not a retag this
        # can follow - there is no record for them to be part of.
        log.info("retag left %s with no album tag; identity unchanged",
                 live[0].parent.name)
        return ""
    new_key = registry.album_key(meta.albumartist, meta.album)

    # An album filed before the registry existed has a UUID on disk and no
    # row. Adopting it first is what makes the repoint a move rather than a
    # mint, so the record keeps the identity Navidrome already knows it by.
    if old_key:
        carried = {album for _, album in map(_read_identity, live) if album}
        if len(carried) == 1:
            registry.uuid_for_key(space.library_id, old_key,
                                  on_miss=carried.pop())

    settled = registry.repoint(space.library_id, old_key or new_key, new_key)

    rewritten = 0
    for path in live:
        had_track, had_album = _read_identity(path)
        if had_track and had_album == settled:
            continue
        _write_identity(path, None if had_track else str(uuid.uuid4()), settled)
        rewritten += 1

    if rewritten:
        log.info("%d file(s) joined album %s after a retag", rewritten, settled)
    return settled


def _write_identity(path: Path, track_uuid: str | None,
                    album_uuid: str | None) -> bool:
    """Write whichever UUIDs are not already right. True if the file has them.

    Both None means the file already carries what it should, which is the
    ordinary case for the second pass over anything - and skipping the write
    is what keeps re-filing idempotent instead of churning mtimes.

    A failure is logged, not raised. A file in the library without a UUID is
    visible and playable and turns up in the review list; a file left in
    scratch space is none of those things.
    """
    if track_uuid is None and album_uuid is None:
        return True
    if not uuidtags.can_carry_tags(path):
        # WAV and AIFF have nowhere to put it. Reporting these as missing a
        # UUID would be noise: nothing can be done short of converting.
        log.debug("%s cannot carry identity tags", path.name)
        return True
    try:
        uuidtags.write(path, track_uuid, album_uuid)
        # Read back rather than assumed. A write that did not survive the
        # round trip is a failure however plausible it looked, and this is
        # the last moment anything looks at the file: once filed it is
        # neither in the inbox nor loose at the library root, so nothing
        # ever comes back to it.
        wrote_track, wrote_album = uuidtags.read(path)
        if track_uuid is not None and wrote_track != track_uuid:
            raise RuntimeError(
                f"track UUID did not survive the write "
                f"(read back {wrote_track!r})")
        if album_uuid is not None and wrote_album != album_uuid:
            raise RuntimeError(
                f"album UUID did not survive the write "
                f"(read back {wrote_album!r})")
        return True
    except Exception as exc:
        log.warning("could not write identity onto %s: %s: %s",
                    path.name, type(exc).__name__, exc)
        return False


def _move_into_place(source: Path, target: Path) -> Path:
    """Move a file to target, never overwriting silently.

    Running out of candidates raises rather than falling through. The loop
    used to leave `target` at the original name when all 98 were taken, so
    the one case the numbering exists to prevent - a real collision - ended
    in os.replace overwriting the file it was protecting.

    `os.replace` is tried first because it is atomic, but the scratch space a
    download is built in and the library it is filed into are separate bind
    mounts under Docker, so most real moves cross a filesystem boundary and
    land on the copy-and-delete fallback.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        stem, suffix = target.stem, target.suffix
        for n in range(2, 100):
            candidate = target.with_name(f"{stem} ({n}){suffix}")
            if not candidate.exists():
                target = candidate
                break
        else:
            raise FileExistsError(
                f"{target} and 98 numbered variants all exist; refusing to "
                f"overwrite. Clear some out of {target.parent}.")
    try:
        os.replace(source, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.move(str(source), str(target))
    return target


def _read_identity(path: Path) -> tuple[str | None, str | None]:
    """The UUIDs already on a file, or (None, None) if it has none to read."""
    try:
        return uuidtags.read(path)
    except uuidtags.UnreadableFile as exc:
        log.debug("%s carries no readable identity (%s)", path.name, exc)
        return None, None


def file_track(space: workspace.Workspace, source: Path) -> Filed:
    """Tag a finished file with its identity and move it into the library.

    Identity is written before the move, so the file appears at its final
    path complete: Navidrome never sees a track without a UUID and then has
    to be told about it again.

    Idempotent. A file already in the right place with the right identity is
    read, not rewritten, and stays where it is - which is what lets the
    migration pass run over the existing library.
    """
    meta = read_meta(source)
    had_track, had_album = _read_identity(source)

    # A track UUID already on the file is never touched - it is what the stars
    # and play counts hang off. The album UUID comes from the registry, which
    # keeps whatever the file already carried if it has never seen that album:
    # inventing a second UUID beside the one on disk would split the record.
    track_uuid = had_track or str(uuid.uuid4())
    key = (registry.album_key(meta.albumartist, meta.album) if meta.names_album
           else registry.loose_key(track_uuid))
    album_uuid = registry.uuid_for_key(space.library_id, key, on_miss=had_album)

    identified = _write_identity(
        source,
        None if had_track else track_uuid,
        None if had_album == album_uuid else album_uuid)

    target = destination(space, meta, source.suffix.lower())
    if source.resolve() != target.resolve():
        target = _move_into_place(source, target)

    return Filed(path=target, track_uuid=track_uuid, album_uuid=album_uuid,
                 album_key=key, identified=identified)
