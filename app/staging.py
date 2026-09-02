"""Decides where finished files go, and gets them there without beets seeing
a half-written album.

Two rules drive everything here. Files are built inside a hidden directory on
the same filesystem as the output tree and moved into place only once the
whole album is finished, so a beets cron firing mid-download never imports a
partial release. And an album is only staged as an album when every one of its
tracks was fetched; the fragments a playlist produces go to a singles
directory instead, because beets cannot album-match two tracks of a twelve
track record and will skip them under --quiet.
"""

from __future__ import annotations

import os
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

from .config import settings

# Characters Windows forbids in a filename, plus control characters.
_ILLEGAL = re.compile(r'[<>:"/\|?*\x00-\x1f]')
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


def incomplete_root(job_id: str) -> Path:
    """Scratch space for a job, on the same filesystem as the output tree.

    It has to live under output_dir rather than in the config directory: those
    are separate volumes under Docker, and os.replace cannot move a directory
    across filesystems atomically.
    """
    return settings.output_dir / ".incomplete" / job_id


def album_folder(item: dict[str, Any]) -> str:
    return sanitize(f"{item['album_artist']} - {item['album']}")


def track_filename(item: dict[str, Any], multi_disc: bool) -> str:
    track = item.get("track_no") or 0
    number = f"{item.get('disc_no') or 1}-{track:02d}" if multi_disc else f"{track:02d}"
    return f"{sanitize(f'{number} - {item['title']}')}.mp3"


def single_filename(item: dict[str, Any]) -> str:
    return f"{sanitize(f'{item['artist']} - {item['title']}')}.mp3"


def plan(job_id: str, items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
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

    root = incomplete_root(job_id)
    layout: dict[str, dict[str, Any]] = {}

    for item in items:
        album_id = item.get("album_id")
        total = item.get("album_total") or 0
        complete = bool(album_id) and total > 0 and held[album_id] >= total

        if complete:
            folder = album_folder(item)
            name = track_filename(item, multi_disc=discs[album_id] > 1)
            temp = root / "albums" / folder / name
            final = settings.albums_dir / folder / name
        else:
            name = single_filename(item)
            temp = root / "singles" / name
            final = settings.singles_dir / name

        layout[item["id"]] = {"temp": temp, "final": final, "complete_album": complete}

    return layout


def demote_partial_albums(job_id: str, items: list[dict[str, Any]],
                          layout: dict[str, dict[str, Any]]) -> int:
    """Reclassify tracks whose album did not download in full.

    Completeness is decided before downloading, so a job planned as a whole
    album can still end up short if a track fails. An album directory holding
    nine of twelve tracks is worse than useless to beets - it will try to
    album-match it and fail - so whatever did arrive is moved into singles,
    where a singleton import handles it correctly.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        planned = layout.get(item["id"])
        if planned and planned["complete_album"]:
            groups.setdefault(item["album_id"], []).append(item)

    moved = 0
    singles_root = incomplete_root(job_id) / "singles"

    for group in groups.values():
        if all(item["status"] == "complete" for item in group):
            continue
        for item in group:
            planned = layout[item["id"]]
            if item["status"] != "complete" or not planned["temp"].exists():
                continue
            target = singles_root / single_filename(item)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(planned["temp"], target)
            planned["temp"] = target
            planned["final"] = settings.singles_dir / target.name
            planned["complete_album"] = False
            item["file_path"] = str(planned["final"])
            moved += 1

    albums = incomplete_root(job_id) / "albums"
    if albums.is_dir():
        for folder in albums.iterdir():
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()

    return moved


def _move_into_place(source: Path, target: Path) -> Path:
    """Move a file or directory to target, never overwriting silently."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        stem, suffix = target.stem, target.suffix
        for n in range(2, 100):
            candidate = target.with_name(f"{stem} ({n}){suffix}")
            if not candidate.exists():
                target = candidate
                break
    os.replace(source, target)
    return target


def publish(job_id: str) -> list[Path]:
    """Move a finished job's output into the beets staging tree.

    Album directories move as a unit so beets only ever sees complete
    releases; singles move file by file.
    """
    root = incomplete_root(job_id)
    if not root.exists():
        return []

    published: list[Path] = []

    albums = root / "albums"
    if albums.is_dir():
        for folder in sorted(albums.iterdir()):
            if folder.is_dir() and any(folder.iterdir()):
                published.append(_move_into_place(folder, settings.albums_dir / folder.name))

    singles = root / "singles"
    if singles.is_dir():
        for track in sorted(singles.iterdir()):
            if track.is_file():
                published.append(_move_into_place(track, settings.singles_dir / track.name))

    shutil.rmtree(root, ignore_errors=True)
    return published


def discard(job_id: str) -> None:
    shutil.rmtree(incomplete_root(job_id), ignore_errors=True)
