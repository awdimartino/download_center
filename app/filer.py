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

import contextlib
import errno
import json
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import registry, uuidtags, workspace

log = logging.getLogger("navidrome_companion.filer")

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
    # Navidrome's scanner skips any folder or file whose name starts with
    # exactly one dot, as hidden. ".38 Special" was filed, reported Done and
    # never appeared. Two or more dots are not hidden ("...And Justice for
    # All"), so only the single one is replaced.
    if cleaned.startswith(".") and not cleaned.startswith(".."):
        cleaned = f"_{cleaned[1:]}"
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

# The tags a person may change, and what they are called through mutagen's
# easy interface - which is the same spelling for MP3, FLAC, MP4 and Vorbis,
# and the only reason this does not need a branch per container format.
_EASY = {"albumartist": "albumartist", "album": "album", "artist": "artist",
         "title": "title", "track_no": "tracknumber", "disc_no": "discnumber"}


class NotEditable(Exception):
    """The file cannot carry the tags being written."""


def check_writable(paths: list[Path]) -> None:
    """Open every file as a tag writer would, before any is written.

    A failure on file k of an album used to leave 0..k-1 retagged,
    unregistered and unmoved - half an album renamed, with nothing to say
    so. Opening them all first turns the common causes (a file that is not
    really audio, one that cannot be written) into a refusal that changes
    nothing. A disk filling mid-write can still interrupt; that is rarer.
    """
    from mutagen import File as MutagenFile

    for path in paths:
        try:
            audio = MutagenFile(path, easy=True)
        except Exception as exc:
            raise NotEditable(f"{path.name}: {type(exc).__name__}: {exc}") from exc
        if audio is None:
            raise NotEditable(f"{path.name} is not a format this can tag")
        if not os.access(path, os.W_OK):
            raise NotEditable(f"{path.name} cannot be written")


def write_tags(path: Path, **fields: Any) -> None:
    """Set the named tags, leaving everything else on the file alone.

    Surgical on purpose. `tagger.tag` deletes the whole frame set before
    writing, which is right for a fresh download and destructive for a file
    somebody is correcting by hand - it would take the cover art, the ISRC
    and the Spotify id with it.

    A number keeps whatever total was written beside it: `discnumber` of
    "1/2" edited to disc 2 becomes "2/2", because dropping the total would
    change how `read_meta` reads the release and rename every file on it.
    `track_total` and `disc_total` replace the totals instead - for a
    combine, which renumbers into a release whose length none of the old
    totals describe.
    """
    from mutagen import File as MutagenFile

    track_total = fields.get("track_total")
    disc_total = fields.get("disc_total")
    wanted = {name: value for name, value in fields.items()
              if value is not None and name in _EASY}
    if not wanted:
        return

    try:
        audio = MutagenFile(path, easy=True)
    except Exception as exc:
        raise NotEditable(f"{type(exc).__name__}: {exc}") from exc
    if audio is None:
        raise NotEditable("not a format this can tag")
    if audio.tags is None:
        audio.add_tags()

    for name, value in wanted.items():
        key = _EASY[name]
        text = str(value).strip()
        if name == "track_no" and track_total:
            text = f"{text}/{int(track_total)}"
        elif name == "disc_no" and disc_total:
            text = f"{text}/{int(disc_total)}"
        elif name in ("track_no", "disc_no"):
            existing = (audio.tags.get(key) or [""])[0]
            _, slash, total = str(existing).partition("/")
            text = f"{text}/{total}" if slash and total else text
        audio.tags[key] = text
    try:
        audio.save()
    except Exception as exc:
        raise NotEditable(f"{type(exc).__name__}: {exc}") from exc


def _saved_tags(path: Path, fields: dict[str, Any]) -> dict[str, list[str] | None]:
    """The values `write_tags` is about to replace, to put back if it must."""
    from mutagen import File as MutagenFile

    audio = MutagenFile(path, easy=True)
    tags = audio.tags if audio is not None and audio.tags is not None else {}
    saved: dict[str, list[str] | None] = {}
    for name in fields:
        key = _EASY.get(name)
        if key is not None:
            value = tags.get(key)
            saved[key] = list(value) if value else None
    return saved


def _restore_tags(path: Path, saved: dict[str, list[str] | None]) -> None:
    from mutagen import File as MutagenFile

    audio = MutagenFile(path, easy=True)
    if audio is None:
        return
    if audio.tags is None:
        audio.add_tags()
    for key, value in saved.items():
        if value is None:
            audio.tags.pop(key, None)
        else:
            audio.tags[key] = value
    audio.save()


# Left in an album's folder while it is being renamed, saying what it was
# called. Hidden, so Navidrome skips it. If the rename is cut short - the
# container stops part way through - the folder holds two names, which an
# album action refuses; this says the second is the first one's rename, so
# renaming again can finish the job instead of splitting the album.
RENAMING = ".renaming"


def _renaming_from(folder: Path) -> str | None:
    try:
        return json.loads((folder / RENAMING).read_text(encoding="utf-8"))["was"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _album_identity(path: Path) -> tuple[str, str, bool]:
    """What this file currently says about which album it is on."""
    meta = read_meta(path)
    return (meta.albumartist, meta.album, meta.names_album)


def _prune_upwards(folder: Path, stop_at: Path) -> None:
    """Remove a directory the files have just left, and its empty parents.

    Bounded by the library root, and it only ever removes a directory that
    is already empty - so a folder still holding cover art or a cue sheet
    stays, which is the right way to be wrong about somebody's files.
    """
    stop = stop_at.resolve()
    here = folder.resolve()
    while here != stop and stop in here.parents:
        try:
            if any(here.iterdir()):
                return
            here.rmdir()
        except OSError:
            return
        here = here.parent


def leave_folder(folder: Path, went_to: set[Path], stop_at: Path) -> None:
    """Tidy a folder the tracks have just left, then prune it if empty.

    A cover is *copied* along with the first track that moves (see
    `_carry_cover`), and beets' fetchart adds one too, so a folder whose
    every track has moved still held its cover.jpg, was never empty, and
    was never pruned: each rename, match or combine left
    OldArtist/OldAlbum/cover.jpg behind. Once no audio is left, a cover is
    removed only if an identical copy now sits where the tracks went - so
    nothing is lost. Anything else (a cue sheet, a rip log) keeps the
    folder, which is the right way to be wrong about somebody's files.
    """
    try:
        if not folder.is_dir() or audio_in(folder):
            return
        for cover in folder_covers(folder):
            data = cover.read_bytes()
            if any((there / cover.name).is_file()
                   and (there / cover.name).read_bytes() == data
                   for there in went_to if there != folder):
                cover.unlink()
    except OSError:
        log.debug("could not tidy %s", folder, exc_info=True)
    _prune_upwards(folder, stop_at)


def retag_album(space: workspace.Workspace, folder: Path,
                **fields: Any) -> list[Filed]:
    """Change what a whole folder says it is, and move it to match.

    Album-level by construction: one album is one UUID, so the artist and
    the album title are properties of the folder, not of a track in it.
    Editing them on a single file is how a record ends up as two.

    The album keeps its UUID, so its Navidrome identity and the stars and
    play counts hanging off it survive - unless the new name is already
    taken, in which case the incumbent wins and these files join it.
    """
    files = audio_in(folder)
    if not files:
        raise NotEditable("there is nothing in that folder to edit")
    interrupted = _renaming_from(folder)
    named, _ = _by_album(files)
    if interrupted and interrupted in named and len(named) <= 2:
        # A rename that was cut short: the old name and the one it was
        # becoming. Finish it, following the album from where it started.
        was = interrupted
    else:
        require_one_album(folder)
        was = album_key_of(folder)
    check_writable(files)

    marker = folder / RENAMING
    if was:
        marker.write_text(json.dumps({"was": was}), encoding="utf-8")
    written: list[tuple[Path, dict[str, list[str] | None]]] = []
    try:
        for path in files:
            saved = _saved_tags(path, fields)
            write_tags(path, **fields)
            written.append((path, saved))
    except Exception:
        # Put back what was already written. A disk filling on file k of
        # an album used to leave 0..k-1 renamed and the rest not: a folder
        # with two names, which every album action then refused.
        for path, saved in reversed(written):
            try:
                _restore_tags(path, saved)
            except Exception:
                log.exception("could not put back the tags on %s", path)
        with contextlib.suppress(OSError):
            marker.unlink()
        raise

    after_retag(space, files, was)
    filed = [file_track(space, path) for path in files]
    with contextlib.suppress(OSError):
        marker.unlink()
    leave_folder(folder, {one.path.parent for one in filed},
                  space.library_path)
    return filed


def retag_track(space: workspace.Workspace, path: Path,
                **fields: Any) -> Filed:
    """Change one file's tags, and move it if they say it belongs elsewhere.

    Changing the title or a number renames it inside its own folder.
    Changing the artist or the album takes it *out* of its album and into
    another - which is the operation that rescues a track filed under the
    wrong record, and the operation that splits one if it is done by
    mistake. Either way the caller has already confirmed it.

    The album UUID on the file is deliberately not carried across. It
    belongs to the album the track is leaving; registering it under the new
    name would fuse the two rather than move the one.
    """
    # Asked of the tags, before and after, rather than of which fields were
    # passed. `read_meta` falls back to the track artist when there is no
    # album artist, so setting the artist of a file in Unknown Album moves
    # it - and a "did they pass album fields" test would miss that.
    before = _album_identity(path)
    write_tags(path, **fields)
    moving = _album_identity(path) != before

    was = path.parent
    # Never the cover: the folder it leaves is the album it is leaving, and
    # that album's art on another album is the wrong picture.
    filed = file_track(space, path, adopt_album=not moving, carry_cover=False)
    if filed.path.parent != was:
        leave_folder(was, {filed.path.parent}, space.library_path)
    return filed


def audio_in(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*")
                  if p.is_file() and uuidtags.is_audio(p))


def _by_album(paths: list[Path]) -> tuple[dict[str, list[Path]], dict[str, str]]:
    """The files grouped by the album key their tags name, with a readable
    label per key. A file with no album tag is its own record and is left
    out: it is not part of any album here."""
    named: dict[str, list[Path]] = {}
    labels: dict[str, str] = {}
    for path in paths:
        meta = read_meta(path)
        if not meta.names_album:
            continue
        key = registry.album_key(meta.albumartist, meta.album)
        named.setdefault(key, []).append(path)
        labels.setdefault(key, f"{meta.albumartist} - {meta.album}")
    return named, labels


def require_one_album(folder: Path) -> None:
    """Refuse a folder-wide action on a folder holding more than one album.

    Album actions treat a folder as one record. Older folders can hold two
    (57 were found), and two names can sanitise to one folder - `AC/DC` and
    `AC_DC`, or a case-insensitive filesystem. Renaming, matching or
    combining such a folder silently absorbed the second album, and its
    registry row was orphaned.
    """
    _, labels = _by_album(audio_in(folder))
    if len(labels) > 1:
        raise NotEditable(
            f"{folder.name} holds more than one album "
            f"({'; '.join(sorted(labels.values()))}). Move the tracks of one "
            "of them out first, or edit them track by track.")


UNIDENTIFIED = ("filed, but its identity tags could not be written, so stars "
                "and play counts cannot follow it")


def unidentified(filed: list[Filed]) -> list[str]:
    """What to say about each filed track whose identity tags did not take.

    Reported, not ignored: a merge or rename whose UUID write failed on some
    files left a split album behind and said "moved" over it.
    """
    return [f"{one.path.name}: {UNIDENTIFIED}" for one in filed
            if not one.identified]


def album_key_of(folder: Path) -> str:
    """The registry key the files in a folder currently answer to.

    Read before a retag, so the album can be followed to whatever it becomes.
    The key the tagged files share, not the first file's: `require_one_album`
    lets an untagged stray through, and when it sorted first there was no
    key to follow, so the whole album was given a new UUID and lost its
    stars and plays. Empty when no file names an album - each is then its
    own record - or, defensively, when they name more than one.
    """
    named, _ = _by_album(audio_in(folder))
    if len(named) != 1:
        return ""
    return next(iter(named))


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

    # Every file is asked, not the first. Beets can leave a file it did not
    # match untouched, and a download can land mid-operation: stamping the
    # settled UUID on those gave a file still tagged as the old album the new
    # album's identity - two names, two folders, one album UUID.
    named, labels = _by_album(live)
    if not named:
        # A retag that did not give these files an album is not a retag this
        # can follow - there is no record for them to be part of.
        log.info("retag left %s with no album tag; identity unchanged",
                 live[0].parent.name)
        return ""
    if len(named) > 1:
        raise NotEditable(
            f"the files in {live[0].parent.name} no longer agree which album "
            f"they are on ({'; '.join(sorted(labels.values()))}), so the "
            "album's identity was left as it was and nothing was moved")
    (new_key, live), = named.items()

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


# What a folder cover is called, in the order Navidrome looks for one.
COVER_NAMES = ("cover", "folder", "front", "album", "albumart")
COVER_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")


def folder_covers(folder: Path) -> list[Path]:
    """Every folder cover in this folder, in Navidrome's order of preference.

    Matched without regard to case: a rip made on Windows says Folder.jpg
    or cover.JPG, and on the Pi's filesystem a lower-case lookup missed it -
    Fetch cover embedded new art and reported success while the file it
    never saw went on being the picture Navidrome showed.
    """
    try:
        files = {p.name.lower(): p for p in folder.iterdir() if p.is_file()}
    except OSError:
        return []
    return [files[f"{name}{suffix}"] for name in COVER_NAMES
            for suffix in COVER_SUFFIXES if f"{name}{suffix}" in files]


def folder_cover(folder: Path) -> Path | None:
    """The folder cover Navidrome would show for this folder, if any."""
    found = folder_covers(folder)
    return found[0] if found else None


def _folder_is_one_album(folder: Path, track: Path, key: str) -> bool:
    """Whether every other track in the folder names the same album.

    Stops at the first that does not, so a dump of a hundred albums in one
    folder costs a few tag reads, not hundreds.
    """
    for other in sorted(folder.iterdir()):
        if other == track or not other.is_file() or not uuidtags.is_audio(other):
            continue
        try:
            meta = read_meta(other)
        except Exception:
            return False
        if not meta.names_album or registry.album_key(meta.albumartist,
                                                      meta.album) != key:
            return False
    return True


def _carry_cover(source_dir: Path, target_dir: Path, track: Path | None = None,
                 key: str | None = None) -> None:
    """Copy a folder cover across with the first track that moves.

    Filing moves audio and nothing else, so an album dropped in as a
    directory used to leave its cover.jpg behind and arrive in the library
    with no art unless something was embedded in the files. Navidrome reads
    a folder cover, so this is the difference between a wall of album
    squares and a wall of grey ones.

    Copied rather than moved: the source folder may still hold tracks that
    have not been filed yet, and the second of them finding no cover would
    be the same bug one file later. Skipped entirely once the destination
    has one, so it happens once per album however many tracks arrive.

    Only into a new folder, and only from a folder that is one album. A
    folder already holding music is an album with its own art, embedded if
    not in a file, and Navidrome prefers a folder cover to embedded art: a
    dropped folder of several albums and one cover.jpg gave that picture to
    every album it touched, existing ones included.

    Never worth failing a file for - the music is what matters, and the art
    can be added later.
    """
    try:
        if not source_dir.is_dir():
            return
        found = folder_cover(source_dir)
        if found is None:
            return
        if target_dir.is_dir():
            if folder_cover(target_dir) is not None:
                return
            if any(p.is_file() and uuidtags.is_audio(p)
                   for p in target_dir.iterdir()):
                return
        if track is not None and key is not None and not _folder_is_one_album(
                source_dir, track, key):
            return
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(found, target_dir / found.name)
        log.info("carried %s across to %s", found.name, target_dir)
    except Exception as exc:
        log.warning("could not carry the cover from %s: %s: %s",
                    source_dir, type(exc).__name__, exc)


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


def unused_name(target: Path) -> Path:
    """`target`, or the first numbered variant of it that is free.

    The one rule about never overwriting a track, in one place. Both roads
    into the library move files - the inbox hop and the filing itself - and
    they each carried a verbatim copy of this loop, 170 lines apart in two
    modules, so a change to the cap or the naming would have applied to one
    of them.

    Running out of candidates raises rather than falling through. The loop
    used to leave `target` at the original name when all 98 were taken, so
    the one case the numbering exists to prevent - a real collision - ended
    in the caller overwriting the file it was protecting.
    """
    for candidate in _variants(target):
        if not candidate.exists():
            return candidate
    raise _all_taken(target)


def _already_there(source: Path, target: Path) -> bool:
    """Whether a file is already where it belongs: the target itself, or one
    of its numbered variants in the same folder.

    A second "Same.mp3" is filed as "Same (2).mp3". Asked only "is it the
    target", that file never was, so every save of its album moved it to
    "(3)" - "(2)" being its own name, and taken - and the next save back.
    """
    if source.resolve() == target.resolve():
        return True
    if source.parent.resolve() != target.parent.resolve():
        return False
    return source.name in {variant.name for variant in _variants(target)}


def _variants(target: Path):
    yield target
    stem, suffix = target.stem, target.suffix
    for n in range(2, 100):
        yield target.with_name(f"{stem} ({n}){suffix}")


def _all_taken(target: Path) -> FileExistsError:
    return FileExistsError(
        f"{target} and 98 numbered variants all exist; refusing to "
        f"overwrite. Clear some out of {target.parent}.")


def claim(target: Path) -> Path:
    """Like `unused_name`, but the name is reserved by creating it empty.

    Choosing a free name and then moving onto it left a gap: two jobs
    filing the same track could both choose it, and the second move
    replaced the first file. Creating the file with O_EXCL is one step that
    only one of them can win; the move then replaces its own placeholder.
    """
    for candidate in _variants(target):
        try:
            os.close(os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            continue
        return candidate
    raise _all_taken(target)


def _move_into_place(source: Path, target: Path) -> Path:
    """Move a file to target, never overwriting silently, and never leaving
    part of one under a real name.

    The scratch space a download is built in and the library it is filed
    into are separate bind mounts under Docker, so most real moves cross a
    filesystem boundary and have to be a copy. That copy used to be made
    onto the claimed name: a full disk or a restart part way through left a
    truncated track there, and the next attempt filed a second copy beside
    it. Now the file arrives next to its destination under a hidden name -
    Navidrome skips names starting with a dot - and only then is renamed
    onto the claimed name, which is one atomic step within a folder.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.parent / f".{uuid.uuid4().hex}.part"
    copied = False
    try:
        os.replace(source, staged)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        try:
            shutil.copy2(source, staged)
        except BaseException:
            with contextlib.suppress(OSError):
                staged.unlink()
            raise
        copied = True

    try:
        claimed = claim(target)
        os.replace(staged, claimed)
    except BaseException:
        # Put things back as they were: a rename goes back where it came
        # from, a copy is dropped and its source is still there.
        with contextlib.suppress(OSError):
            if copied:
                staged.unlink()
            else:
                os.replace(staged, source)
        raise
    if copied:
        with contextlib.suppress(FileNotFoundError):
            source.unlink()
    return claimed


def _album_uuid_on_disk(folder: Path, key: str, source: Path) -> str | None:
    """The album UUID the files already in `folder` under `key` agree on.

    None when there are none, or they disagree, or any of them carries none:
    an ambiguous answer is worse than a fresh UUID, which the disk audit
    will at least report.
    """
    if not folder.is_dir():
        return None
    found: set[str | None] = set()
    for path in audio_in(folder):
        if path == source or path.parent != folder:
            continue
        try:
            meta = read_meta(path)
        except Exception:
            return None
        if not meta.names_album or registry.album_key(meta.albumartist,
                                                      meta.album) != key:
            continue
        found.add(_read_identity(path)[1])
    if len(found) == 1:
        return found.pop()
    return None


def _read_identity(path: Path) -> tuple[str | None, str | None]:
    """The UUIDs already on a file, or (None, None) if it has none to read."""
    try:
        return uuidtags.read(path)
    except uuidtags.UnreadableFile as exc:
        log.debug("%s carries no readable identity (%s)", path.name, exc)
        return None, None


def file_track(space: workspace.Workspace, source: Path,
               adopt_album: bool = True, carry_cover: bool = True) -> Filed:
    """Tag a finished file with its identity and move it into the library.

    Identity is written before the move, so the file appears at its final
    path complete: Navidrome never sees a track without a UUID and then has
    to be told about it again.

    Idempotent. A file already in the right place with the right identity is
    read, not rewritten, and stays where it is - which is what lets the
    migration pass run over the existing library.

    `adopt_album` is what makes a file arriving with an album UUID keep it:
    a re-file of music that predates the registry should not have a second
    UUID invented beside the one already on disk. It has to be turned off
    when the album tags were *deliberately changed*, because then the UUID
    on the file belongs to the album the track is leaving - and registering
    it under the new key would fuse two records rather than move one track.
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
    target = destination(space, meta, source.suffix.lower())
    on_miss = had_album if adopt_album else None
    if (on_miss is None and meta.names_album
            and registry.known(space.library_id, key) is None):
        # Never seen under this name, but the album may be on disk already:
        # retagged by another tool, or a key the registry lost. Minting
        # beside it split the album, and the next Save album moved every
        # file to the newcomer's UUID, taking the album's stars and plays.
        on_miss = _album_uuid_on_disk(target.parent, key, source)
    album_uuid = registry.uuid_for_key(space.library_id, key, on_miss=on_miss)

    identified = _write_identity(
        source,
        None if had_track else track_uuid,
        None if had_album == album_uuid else album_uuid)

    if not _already_there(source, target):
        # Only from a folder that belongs to the music. Downloads are filed
        # from the inbox root, so a stray cover.jpg there was copied into
        # every later album with no folder cover of its own - and Navidrome
        # prefers a folder cover to the right embedded art. The library
        # root is the same case for a loose file.
        if carry_cover and source.parent.resolve() not in (
                space.inbox_dir.resolve(), space.library_path.resolve()):
            _carry_cover(source.parent, target.parent, source, key)
        target = _move_into_place(source, target)
    else:
        # Staying put, under whatever number it already has.
        target = source

    return Filed(path=target, track_uuid=track_uuid, album_uuid=album_uuid,
                 album_key=key, identified=identified)
