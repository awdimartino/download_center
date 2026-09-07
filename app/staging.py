"""Decides where finished files go, and gets them there without beets seeing
a half-written album.

Two rules drive everything here. Files are built inside a hidden directory on
the same filesystem as the output tree and moved into place only once the
whole album is finished, so a beets cron firing mid-download never imports a
partial release. And a track is staged under the album it says it is on,
whole album or not: a singleton import files to `Non-Album/$artist/$title`
regardless of the album tag, so sending fragments to singles scattered them
away from the record they belong to. Beets will still refuse to match a
fragment unattended - it cannot album-match two tracks of a twelve track
record under --quiet - so those wait in staging until someone picks the
release. Only a file with no album at all is really a single.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

from . import workspace

log = logging.getLogger("download_center.staging")

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


def sanitize(name: str) -> str:
    """Make a string safe as a single path component on Windows and Linux."""
    cleaned = _ILLEGAL.sub("_", name or "").strip()
    cleaned = _TRAILING.sub("", cleaned)
    if len(cleaned) > MAX_COMPONENT:
        cleaned = cleaned[:MAX_COMPONENT].rstrip(" .")
    if cleaned.split(".")[0].lower() in _RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned or "unknown"


def incomplete_root(space: workspace.Workspace, job_id: str) -> Path:
    """Scratch space for a job, on the same filesystem as its destination.

    It has to live under the staging tree rather than in the config
    directory: those are separate volumes under Docker, and os.replace cannot
    move a directory across filesystems atomically.
    """
    return space.incomplete_dir / job_id


def album_folder(item: dict[str, Any]) -> str:
    return sanitize(f"{item['album_artist']} - {item['album']}")


def track_filename(item: dict[str, Any], multi_disc: bool) -> str:
    track = item.get("track_no") or 0
    number = f"{item.get('disc_no') or 1}-{track:02d}" if multi_disc else f"{track:02d}"
    return f"{sanitize(f'{number} - {item['title']}')}.mp3"


def single_filename(item: dict[str, Any]) -> str:
    return f"{sanitize(f'{item['artist']} - {item['title']}')}.mp3"


def plan(space: workspace.Workspace, job_id: str,
         items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Work out, for every item, where it is built and where it ends up.

    Returns item id -> {"temp", "final", "complete_album"}.
    """
    held = Counter(item["album_id"] for item in items if item.get("album_id"))
    discs = Counter()
    for item in items:
        if item.get("album_id"):
            discs[item["album_id"]] = max(
                discs[item["album_id"]], item.get("disc_no") or 1
            )

    root = incomplete_root(space, job_id)
    layout: dict[str, dict[str, Any]] = {}

    for item in items:
        album_id = item.get("album_id")
        total = item.get("album_total") or 0
        complete = bool(album_id) and total > 0 and held[album_id] >= total

        # An album folder for anything that says which album it is on, whole
        # or not. Only completeness used to earn one, and a fragment went to
        # singles - which files it to `Non-Album/$artist/$title`, because a
        # singleton import ignores the album tag entirely. So the two tracks
        # of a record that a playlist happened to include ended up nowhere
        # near the rest of it.
        #
        # Now they stay together and, if beets will not match a fragment
        # unattended, wait in staging where the release can be chosen by
        # hand. `complete_album` is still reported: the caller uses it to say
        # what was published, and beets is far likelier to file a whole one.
        if album_id and item.get("album"):
            folder = album_folder(item)
            name = track_filename(item, multi_disc=discs[album_id] > 1)
            temp = root / "albums" / folder / name
            final = space.albums_dir / folder / name
        else:
            name = single_filename(item)
            temp = root / "singles" / name
            final = space.singles_dir / name

        layout[item["id"]] = {"temp": temp, "final": final, "complete_album": complete}

    return layout


def _move_into_place(source: Path, target: Path) -> Path:
    """Move a file or directory to target, never overwriting silently.

    Running out of candidates raises rather than falling through. The loop
    used to leave `target` at the original name when all 98 were taken, so
    the one case the numbering exists to prevent - a real collision - ended
    in os.replace overwriting the file it was protecting.
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
    os.replace(source, target)
    return target


def publish(space: workspace.Workspace, job_id: str) -> list[Path]:
    """Move a finished job's output into the beets staging tree.

    Album directories move as a unit so beets only ever sees complete
    releases; singles move file by file.
    """
    root = incomplete_root(space, job_id)
    if not root.exists():
        return []

    published: list[Path] = []

    albums = root / "albums"
    if albums.is_dir():
        for folder in sorted(albums.iterdir()):
            if folder.is_dir() and any(folder.iterdir()):
                published.append(_move_into_place(folder, space.albums_dir / folder.name))

    singles = root / "singles"
    if singles.is_dir():
        for track in sorted(singles.iterdir()):
            if track.is_file():
                published.append(_move_into_place(track, space.singles_dir / track.name))

    shutil.rmtree(root, ignore_errors=True)
    return published


def discard(space: workspace.Workspace, job_id: str) -> None:
    shutil.rmtree(incomplete_root(space, job_id), ignore_errors=True)


# --- one folder per album ---------------------------------------------------

AUDIO_SUFFIXES = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aiff"}


def _album_of(path: Path) -> tuple[str, str] | None:
    """(album artist, album) from the file's own tags, or None if untagged.

    The album artist, not the track artist: a guest on one track must not
    split an album in two, which is the same trap that made beets read a
    downloaded Thriller as a Various Artists compilation.
    """
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(path, easy=True)
    except Exception:
        return None
    if audio is None or audio.tags is None:
        return None
    album = (audio.tags.get("album") or [""])[0].strip()
    if not album:
        return None
    artist = ((audio.tags.get("albumartist") or [""])[0].strip()
              or (audio.tags.get("artist") or [""])[0].strip()
              or "Unknown Artist")
    return artist, album


def _canonical_artists(albums: dict[str, set[str]]) -> dict[tuple[str, str], str]:
    """One album artist per album, collapsing the featured-artist variants.

    The same trap as the `artist` tag, one level up: a dump of these files
    carries "Drake", "Drake, Detail" and "Drake, JAŸ-Z" as the *album*
    artist of one record, and grouping on the raw string files Nothing Was
    The Same into three folders. Measured on the real thing: 123 folders for
    102 albums.

    Two artists are the same when one is the other with names appended -
    "Drake, Detail" starts with "Drake,". Nothing is split on a comma,
    because "Tyler, The Creator" is one artist with a comma in it, and a
    genuinely multi-artist record - a soundtrack with three composers -
    shares no prefix and stays separate, which is the safe way to be wrong:
    the parts wait for review rather than being merged into a record that
    does not exist.
    """
    canonical: dict[tuple[str, str], str] = {}
    for album, artists in albums.items():
        for artist in artists:
            root = min(
                (other for other in artists
                 if artist == other or artist.startswith(f"{other},")),
                key=len)
            canonical[(album, artist)] = root
    return canonical


def regroup(space: workspace.Workspace) -> dict[str, int]:
    """Put every staged file in a folder named for the album it says it is on.

    The tags decide the layout, not the route the file took to get here.
    Everything downloaded through this application carries Spotify's album
    and album-artist, so tracks of one record group correctly whether they
    arrived as a whole album, as a playlist, or dropped into staging by hand.

    That last case is why this exists. A folder of 746 loose tracks spanning
    a hundred albums is not an album, but staging treated it as one, handed
    the lot to beets as a single release, and beets spent the whole sweep
    failing to match it. Grouped, the complete albums in there file
    themselves and the rest can be looked at one record at a time.

    A file with no album tag is a single, and stays one.

    Idempotent: a file already in the right folder is not touched, so this
    can run before every import.
    """
    staged: list[tuple[Path, tuple[str, str] | None]] = []
    albums: dict[str, set[str]] = {}
    for parent in (space.albums_dir, space.singles_dir):
        if not parent.is_dir():
            continue
        for path in sorted(parent.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in AUDIO_SUFFIXES:
                continue
            # Hidden *directories* are skipped; a file is judged by its
            # suffix alone. `name.startswith(".")` looked like a dotfile
            # check and also skipped two real tracks called "..." and
            # "... (Continued)", which would have sat in staging for ever
            # without ever being looked at.
            if any(part.startswith(".")
                   for part in path.relative_to(parent).parts[:-1]):
                continue
            album = _album_of(path)
            staged.append((path, album))
            if album is not None:
                albums.setdefault(album[1], set()).add(album[0])

    canonical = _canonical_artists(albums)

    grouped = singled = 0
    for path, album in staged:
        if album is None:
            target_dir = space.singles_dir
        else:
            artist = canonical.get((album[1], album[0]), album[0])
            target_dir = space.albums_dir / sanitize(f"{artist} - {album[1]}")
        if path.parent == target_dir:
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        _move_into_place(path, target_dir / path.name)
        if target_dir == space.singles_dir:
            singled += 1
        else:
            grouped += 1

    # Folders emptied by the regrouping. Left behind they would be handed to
    # beets as albums with nothing in them.
    for parent in (space.albums_dir, space.singles_dir):
        if not parent.is_dir():
            continue
        for entry in sorted(parent.rglob("*"), reverse=True):
            if entry.is_dir() and not any(entry.iterdir()):
                entry.rmdir()

    if grouped or singled:
        log.info("regrouped %d file(s) into albums and %d into singles for %s",
                 grouped, singled, space.username)
    return {"grouped": grouped, "singles": singled}
