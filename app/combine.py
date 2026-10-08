"""Several albums and loose tracks, made into one album.

Before this, combining meant renaming each album in turn to exactly the name
of the one it should join, one row at a time, and moving loose tracks over
one by one from behind "More". A YouTube download arrives as a single - an
album of one, in its own folder - so five songs from one record were five
renames, each with its own chance of a typo that founded a sixth album.

Everything here is the filer's existing retag, in a deliberate order:

1. The album that keeps its identity goes first. Renamed only if it has to
   be, so its UUID moves with its new name and album-level stars and play
   counts stay on it.
2. Every other album joins it. The registry already has the target's key by
   then, so each one adopts the target's UUID rather than founding its own.
3. Loose tracks are moved one at a time, the same way.
4. Only then are they renumbered, by the order the person set, and the
   chosen cover goes on every file.

Track UUIDs are never touched, so per-track stars and play counts survive
whatever happens to the albums around them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import covers, filer, workspace

log = logging.getLogger("navidrome_companion.combine")


def combine(space: workspace.Workspace, *,
            albumartist: str, album: str,
            albums: list[Path], tracks: list[Path],
            keep: Path | None = None,
            order: list[Path] | None = None,
            cover: bytes | None = None,
            report: Callable[..., None] | None = None) -> dict[str, Any]:
    """Make `albums` (folders) and `tracks` (files) into one album.

    `keep` is the album whose identity survives - one of `albums`, or None
    to let the first of them carry it. If an album named `albumartist` /
    `album` already exists and is not among these, everything joins that
    one instead, because the registry has its key.

    `order`, when given, is every file in the order it should be numbered.
    A file not in it keeps its number. `cover` goes on every file once they
    are together; read it before calling, since a source folder is gone by
    the time this returns.
    """
    report = report or (lambda **_: None)
    albums = [folder for folder in albums if folder not in (keep,)]
    if keep is not None:
        albums.insert(0, keep)
    inside = {folder.resolve() for folder in albums}
    tracks = [path for path in tracks if path.resolve().parent not in inside]

    total = sum(len(filer.audio_in(folder)) for folder in albums) + len(tracks)
    moved: dict[Path, Path] = {}
    failed: list[str] = []
    album_uuid = ""
    done = 0

    def step(label: str) -> None:
        nonlocal done
        done += 1
        report(done=done, total=total, album=label)

    for folder in albums:
        files = filer.audio_in(folder)
        meta = filer.read_meta(files[0]) if files else None
        if meta and meta.names_album and (meta.albumartist, meta.album) == (albumartist, album):
            # Already called that: renaming it to itself would only rewrite
            # every file for nothing. It is still re-filed, though - its
            # folder may not be the canonical one the others are joining,
            # and skipping it left one album UUID across two folders.
            for path in files:
                try:
                    filed = filer.file_track(space, path)
                except (filer.NotEditable, OSError) as exc:
                    failed.append(f"{path.name}: {exc}")
                    continue
                failed.extend(filer.unidentified([filed]))
                moved[path] = filed.path
                album_uuid = album_uuid or filed.album_uuid
                step(path.name)
            filer.leave_folder(folder, {p.parent for p in moved.values()},
                               space.library_path)
            continue
        try:
            filed = filer.retag_album(space, folder,
                                      albumartist=albumartist, album=album)
        except (filer.NotEditable, OSError) as exc:
            # OSError too: a full disk or 98 taken names ended the whole
            # combine half done, before the scan and the summary of what
            # had already moved.
            failed.append(f"{folder.name}: {exc}")
            continue
        failed.extend(filer.unidentified(filed))
        # retag_album files exactly the list audio_in gave it, in order.
        for before, after in zip(files, filed, strict=True):
            moved[before] = after.path
            album_uuid = album_uuid or after.album_uuid
            step(before.name)

    for path in tracks:
        try:
            filed = filer.retag_track(space, path,
                                      albumartist=albumartist, album=album)
        except (filer.NotEditable, OSError) as exc:
            failed.append(f"{path.name}: {exc}")
            continue
        failed.extend(filer.unidentified([filed]))
        moved[path] = filed.path
        album_uuid = album_uuid or filed.album_uuid
        step(path.name)

    if order:
        numbered = [moved[path] for path in order if path in moved]
        for number, path in enumerate(numbered, start=1):
            try:
                # One sequence is one disc. Keeping the old disc numbers made
                # a two-disc album plus a single into disc 2 from track 11.
                filed = filer.retag_track(space, path, track_no=number,
                                          track_total=len(numbered),
                                          disc_no=1, disc_total=1)
            except (filer.NotEditable, OSError) as exc:
                failed.append(f"{path.name}: {exc}")
                continue
            # Renamed in its folder, so anything after this needs the new path.
            for before, after in moved.items():
                if after == path:
                    moved[before] = filed.path

    landed = sorted({path.parent for path in moved.values() if path.is_file()})
    cover_result = None
    if cover and landed:
        for folder in landed:
            cover_result = covers.apply(folder, filer.audio_in(folder), cover)
            failed.extend(cover_result["failed"])

    return {
        "ran": True,
        "albumartist": albumartist,
        "album": album,
        "moved": len(moved),
        "failed": failed,
        "album_uuid": album_uuid,
        "folder": _relative(space, landed[0]) if landed else None,
        "cover": bool(cover_result and cover_result["written"]),
    }


def _relative(space: workspace.Workspace, folder: Path) -> str:
    try:
        rel = folder.resolve().relative_to(space.library_path.resolve())
    except ValueError:
        return folder.name
    return str(rel).replace("\\", "/")


def guess_album(artist: str, titles: list[str]) -> dict[str, Any] | None:
    """Which album these songs are probably from, asked of Spotify.

    Every title is looked up by itself and votes for each full album it
    appears on - singles and EPs do not count, since they are what is being
    combined. Albums are told apart by name rather than id, so a deluxe and
    a standard edition of the same record vote together.

    None when Spotify is not set up, does not answer, or no album gets a
    vote from more than one song - a guess built on one song is the single
    itself as often as not.
    """
    from . import spotify

    votes: dict[str, dict[str, Any]] = {}
    asked = [t for t in titles if t.strip()][:8]
    for title in asked:
        try:
            found = spotify.search(f'track:"{title}" artist:"{artist}"',
                                   "track", limit=10)
        except Exception as exc:
            log.debug("spotify guess failed for %s: %s", title, exc)
            return None
        seen: set[str] = set()
        for item in found:
            release = item.get("album") or {}
            if release.get("album_type") not in ("album", "compilation"):
                continue
            name = (release.get("name") or "").strip()
            key = name.casefold()
            if not key or key in seen:
                continue
            seen.add(key)
            vote = votes.setdefault(key, {
                "album": name,
                "artist": ", ".join(a["name"] for a in release.get("artists") or []
                                    if a.get("name")) or artist,
                "year": (release.get("release_date") or "")[:4] or None,
                "cover": ((release.get("images") or [{}])[0]).get("url"),
                "votes": 0,
            })
            vote["votes"] += 1

    if not votes:
        return None
    best = max(votes.values(), key=lambda v: v["votes"])
    if len(asked) > 1 and best["votes"] < 2:
        return None
    return best
