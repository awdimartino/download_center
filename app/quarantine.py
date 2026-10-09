"""The Quarantine page: what has been set aside, putting it back, and
letting it go for good.

Setting a file aside has two roads - losing a duplicate pair
(`duplicates.resolve`) and being removed by hand from the Library
(`duplicates.quarantine_one`) - and one destination: `quarantine/` (called
`duplicates-removed/` until 2026-10-09) inside the library it came from, at the same path it had there, with a row
in `duplicate_quarantined` saying where it came from and who moved it.

**Reading.** The disk, with the ledger joined on, as the old read-only list
in Duplicates did: a file an older version set aside, or one moved there by
hand, has no row and is still worth seeing. Its tags say what it is.

**Restoring.** Back to the path it came from, which is what Navidrome knows
it by - its track UUID never changed, so its stars and plays come back with
it. When that path is taken (a duplicate's keeper can have the same name),
it is filed by its tags instead, the way a download is. A restored loser of
a duplicate pair is a duplicate again; the page says so before it is done.

**Deleting.** Gone for good, after a confirmation in the page. The ledger
row stays, stamped, as the record that the file existed.

Every path arrives from the browser as a key, `<library id>:<path inside
the library>`, and is only acted on once it resolves to a file inside the
quarantine of a library this person can see.
"""

from __future__ import annotations

import collections
import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import duplicates, filer, navidrome, store, uuidtags, workspace
from .walk import NDIGNORE, OLD_QUARANTINE_NAMES, QUARANTINE_NAME, QUARANTINE_NAMES

log = logging.getLogger("navidrome_companion.quarantine")

REASONS = {"duplicate": "Lost to a duplicate", "removed": "Removed by hand",
           "unknown": "No record"}


def _libraries(identity: navidrome.Identity) -> list[tuple[dict[str, Any], Path, Path]]:
    """(library, its root, a quarantine folder) for each quarantine folder of
    each library this person can see: the current one, and an old-named one
    while it is still there (a library not mounted when it was renamed)."""
    found = []
    for library in identity.libraries:
        root = Path(library["path"])
        for name in QUARANTINE_NAMES:
            if name == QUARANTINE_NAME or (root / name).is_dir():
                found.append((library, root, root / name))
    return found


def rename_old_folders(roots: list[Path]) -> int:
    """Rename each library's `duplicates-removed/` to `quarantine/`, once.

    One rename on one filesystem, so it is all or nothing, and the empty
    .ndignore inside travels with it: Navidrome never sees the music in
    between. The ledger's paths follow, or a restore would look for the
    files where they were. When both folders exist - a library that already
    gained a new one - the old one's contents are moved across one entry at
    a time, and anything whose name is taken stays where it is.
    """
    moved = 0
    for root in roots:
        new = root / QUARANTINE_NAME
        for name in OLD_QUARANTINE_NAMES:
            old = root / name
            if not old.is_dir():
                continue
            try:
                if not new.exists():
                    old.rename(new)
                else:
                    for entry in list(old.iterdir()):
                        if entry.name in (NDIGNORE, duplicates.QUARANTINE_README):
                            continue
                        if not (new / entry.name).exists():
                            entry.rename(new / entry.name)
                    if not any(e for e in old.iterdir()
                               if e.name not in (NDIGNORE, duplicates.QUARANTINE_README)):
                        shutil.rmtree(old)
            except OSError as exc:
                log.warning("could not rename %s to %s: %s", old, new, exc)
                continue
            duplicates._quarantine_root(root)   # the marker, and the new README
            moved += _repoint_ledger(old, new)
            log.info("renamed %s to %s", old, new)
    return moved


def _repoint_ledger(old: Path, new: Path) -> int:
    """Rewrite recorded paths under `old` to the same place under `new` -
    only where the file is now there, so a row is never pointed at nothing."""
    changed = []
    for row in store.quarantined(limit=None, include_restored=True):
        try:
            inside = Path(row["target_path"] or "").relative_to(old)
        except ValueError:
            continue
        moved = new / inside
        if moved.exists():
            changed.append((str(moved), row["id"]))
    if changed:
        with store.transaction() as tx:
            tx.executemany(
                "UPDATE duplicate_quarantined SET target_path = ? WHERE id = ?", changed)
    return len(changed)


def original_path(rel: Path) -> Path:
    """Where a file inside the quarantine sat in the library.

    The quarantine keeps each file's library path. Before the .ndignore
    marker worked, files were set aside again from inside it, so some sit
    under duplicates-removed/duplicates-removed/...; those leading levels,
    under either name, are not part of where they came from.
    """
    parts = list(rel.parts)
    while parts and parts[0] in QUARANTINE_NAMES:
        parts.pop(0)
    return Path(*parts) if parts else rel


def _active_rows() -> dict[str, dict[str, Any]]:
    """Ledger rows for files still set aside, by where they were put."""
    return {row["target_path"]: row for row in store.quarantined(limit=None)}


def _reason(row: dict[str, Any] | None) -> str:
    if row is None:
        return "unknown"
    return "removed" if (row["group_key"] or "").startswith("manual:") else "duplicate"


def _stamp(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(timespec="seconds")
    except OSError:
        return ""


def listing(identity: navidrome.Identity) -> dict[str, Any]:
    """Every set-aside track, grouped by the album folder it came from,
    newest first."""
    rows = _active_rows()
    albums: dict[tuple[int, str], dict[str, Any]] = {}
    counts: collections.Counter = collections.Counter()
    total_bytes = 0
    for library, root, qroot in _libraries(identity):
        if not qroot.is_dir():
            continue
        for path in sorted(qroot.rglob("*")):
            if not path.is_file() or not uuidtags.is_audio(path):
                continue
            rel = path.relative_to(qroot)
            was = original_path(rel)
            row = rows.get(str(path))
            if row is not None:
                title, artist, album = row["title"], row["artist"], row["album"]
            else:
                try:
                    meta = filer.read_meta(path)
                    title, artist, album = meta.title, meta.albumartist, meta.album
                except Exception:
                    title, artist, album = path.stem, "", ""
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            reason = _reason(row)
            counts[reason] += 1
            total_bytes += size
            entry = {
                "key": f"{library['id']}:{path.relative_to(root).as_posix()}",
                "name": path.name, "was": was.as_posix(),
                "title": title or path.stem, "artist": artist or "",
                "album": album or "", "reason": reason,
                "kept": (row or {}).get("keeper_path") or "",
                "decided_by": (row or {}).get("decided_by") or "",
                "moved_at": (row or {}).get("moved_at") or _stamp(path),
                "size": size,
            }
            folder = was.parent.as_posix()
            group = albums.setdefault((library["id"], folder), {
                "key": f"{library['id']}:{folder}", "library_id": library["id"],
                "library": library["name"], "folder": folder, "tracks": []})
            group["tracks"].append(entry)

    out = []
    for group in albums.values():
        tracks = group["tracks"]
        names = collections.Counter((t["artist"], t["album"]) for t in tracks)
        (artist, album), _ = names.most_common(1)[0]
        tracks.sort(key=lambda t: t["name"])
        out.append({**group, "artist": artist, "album": album,
                    "moved_at": max(t["moved_at"] for t in tracks),
                    "size": sum(t["size"] for t in tracks),
                    "reasons": sorted({t["reason"] for t in tracks})})
    out.sort(key=lambda g: g["moved_at"], reverse=True)
    return {"albums": out, "tracks": sum(counts.values()), "bytes": total_bytes,
            "by_reason": dict(counts)}


# --- acting on keys -------------------------------------------------------------

def _resolve(identity: navidrome.Identity,
             key: str) -> tuple[dict[str, Any], Path, Path, Path]:
    """(library, library root, quarantine root, file) for a key, or ValueError.
    The only door through which a browser's path reaches the disk."""
    library_id, sep, rel = (key or "").partition(":")
    if not sep or not rel:
        raise ValueError(f"{key}: not a quarantined track")
    seen_library = False
    for library, root, qroot in _libraries(identity):
        if str(library["id"]) != library_id:
            continue
        seen_library = True
        path = root / rel
        try:
            inside = qroot.resolve() in path.resolve().parents
        except OSError:
            inside = False
        if inside and path.is_file() and uuidtags.is_audio(path):
            return library, root, qroot, path
    if seen_library:
        raise ValueError(f"{rel}: not a quarantined track")
    raise ValueError(f"{rel}: not in one of your libraries")


def _tidy(folder: Path, qroot: Path, covers_to: Path | None) -> None:
    """A quarantine folder its tracks have left: its covers go back with the
    album (or, after a delete, go too), then it and its emptied parents go.
    Anything else - a cue sheet, a log - keeps its folder."""
    try:
        if not folder.is_dir() or filer.audio_in(folder):
            return
        for cover in filer.folder_covers(folder):
            if covers_to is not None and not (covers_to / cover.name).exists() \
                    and covers_to.is_dir():
                shutil.move(str(cover), str(covers_to / cover.name))
            else:
                cover.unlink()
    except OSError:
        log.debug("could not tidy %s", folder, exc_info=True)
    stop = qroot.resolve()
    here = folder.resolve()
    while here != stop and stop in here.parents:
        try:
            if any(here.iterdir()):
                return
            here.rmdir()
        except OSError:
            return
        here = here.parent


def restore(identity: navidrome.Identity, keys: list[str]) -> dict[str, Any]:
    """Put set-aside tracks back where they came from, or where their tags
    say when that place is taken."""
    rows = _active_rows()
    restored, failed, stamped = [], [], []
    left: dict[Path, tuple[Path, Path]] = {}      # quarantine folder -> (qroot, went to)
    spaces: dict[int, workspace.Workspace] = {}
    for key in keys:
        try:
            library, root, qroot, path = _resolve(identity, key)
            row = rows.get(str(path))
            original = root / original_path(path.relative_to(qroot))
            if row and row.get("source_path"):
                recorded = Path(row["source_path"])
                if root.resolve() in recorded.resolve().parents \
                        and qroot.resolve() not in recorded.resolve().parents:
                    original = recorded
            if original.exists():
                # Taken - by the copy kept in its place, most often. Filed
                # by its tags, as a download would be, under a new name if
                # it has to be.
                if library["id"] not in spaces:
                    spaces[library["id"]] = workspace.for_session(identity, library["id"])
                went = filer.file_track(spaces[library["id"]], path, carry_cover=False).path
            else:
                original.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(original))
                went = original
            left[path.parent] = (qroot, went.parent)
            if row:
                stamped.append(row["id"])
            restored.append({"key": key, "to": went.relative_to(root).as_posix()})
        except Exception as exc:
            failed.append(f"{key.partition(':')[2]}: {exc}")
    store.stamp_quarantine(stamped, "restored")
    for folder, (qroot, went) in left.items():
        _tidy(folder, qroot, went)
    log.info("restored %d track(s) for %s, %d failed", len(restored),
             identity.username, len(failed))
    return {"restored": restored, "failed": failed}


def delete(identity: navidrome.Identity, keys: list[str]) -> dict[str, Any]:
    """Delete set-aside tracks for good."""
    rows = _active_rows()
    deleted, failed, stamped = [], [], []
    left: dict[Path, Path] = {}
    for key in keys:
        try:
            _library, _root, qroot, path = _resolve(identity, key)
            row = rows.get(str(path))
            path.unlink()
            left[path.parent] = qroot
            if row:
                stamped.append(row["id"])
            deleted.append(key)
        except Exception as exc:
            failed.append(f"{key.partition(':')[2]}: {exc}")
    store.stamp_quarantine(stamped, "deleted")
    for folder, qroot in left.items():
        _tidy(folder, qroot, None)
    log.info("deleted %d quarantined track(s) for %s, %d failed", len(deleted),
             identity.username, len(failed))
    return {"deleted": deleted, "failed": failed}


def empty(identity: navidrome.Identity, older_than_days: int) -> dict[str, Any]:
    """Delete everything set aside more than `older_than_days` days ago."""
    cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat(timespec="seconds")
    keys = [track["key"] for album in listing(identity)["albums"]
            for track in album["tracks"] if track["moved_at"] and track["moved_at"] < cutoff]
    return delete(identity, keys)
