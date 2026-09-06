#!/usr/bin/env python3
"""Assign a stable, self-generated identity tag to every audio file.

Navidrome derives a track's identity from a configurable set of tags. Left on
its defaults it uses the MusicBrainz recording id, falling back to album,
disc, track and title when that is absent - and that fallback shifts whenever
a file is retagged, which is exactly when identity most needs to hold still.

Writing our own UUID removes the dependence on match quality. Identity becomes
a single binary fact: the tag is either present and unchanged, or it is not.

The tool is deliberately conservative:

  * an existing UUID is never regenerated or overwritten, only read
  * every write is re-read from disk and verified before the file counts as done
  * a verification failure is reported, never swallowed
  * nothing is written at all without --apply

Album UUIDs are shared by directory. One directory is taken to be one album,
which is how the library is laid out; a file added to that directory later
inherits the existing album UUID from its siblings rather than starting a new
one, so albums do not split in two.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    from mutagen import File as MutagenFile
    # Imported to fail fast with a sentence a person can act on, rather than
    # with a traceback from the middle of a run. Not all are referenced below;
    # that is the point of importing them here.
    from mutagen.flac import FLAC  # noqa: F401
    from mutagen.id3 import ID3, TXXX
    from mutagen.mp3 import MP3  # noqa: F401
    from mutagen.mp4 import MP4
    from mutagen.oggvorbis import OggVorbis  # noqa: F401
except ImportError:
    sys.exit("mutagen is required:  pip3 install --user mutagen")

TRACK_KEY = "NAVIDROME_UUID"
ALBUM_KEY = "NAVIDROME_ALBUM_UUID"

# MP4 freeform atoms are namespaced; these are the exact atom names used.
MP4_TRACK = "----:com.navidrome:UUID"
MP4_ALBUM = "----:com.navidrome:ALBUM_UUID"

AUDIO_SUFFIXES = {".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".oga", ".opus"}


class TagError(Exception):
    """Raised when a file cannot be read, written, or verified."""


# --- per-format read/write -------------------------------------------------

def _read(path: Path) -> tuple[str | None, str | None]:
    """Return (track_uuid, album_uuid) currently on the file."""
    suffix = path.suffix.lower()

    if suffix == ".mp3":
        try:
            tags = ID3(path)
        except Exception:
            return None, None
        def get(key):
            for frame in tags.getall("TXXX"):
                if frame.desc.upper() == key:
                    return str(frame.text[0]) if frame.text else None
            return None
        return get(TRACK_KEY), get(ALBUM_KEY)

    if suffix in (".flac", ".ogg", ".oga", ".opus"):
        audio = MutagenFile(path)
        if audio is None or audio.tags is None:
            return None, None
        def get(key):
            value = audio.tags.get(key) or audio.tags.get(key.lower())
            return str(value[0]) if value else None
        return get(TRACK_KEY), get(ALBUM_KEY)

    if suffix in (".m4a", ".mp4"):
        audio = MP4(path)
        def get(atom):
            value = audio.tags.get(atom) if audio.tags else None
            if not value:
                return None
            raw = value[0]
            return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        return get(MP4_TRACK), get(MP4_ALBUM)

    raise TagError(f"unsupported format: {suffix}")


def _write(path: Path, track_uuid: str, album_uuid: str) -> None:
    suffix = path.suffix.lower()

    if suffix == ".mp3":
        try:
            tags = ID3(path)
        except Exception:
            # A file with no ID3 header at all still needs one.
            tags = ID3()
        # Remove only our own frames, never anything else on the file.
        for key in (TRACK_KEY, ALBUM_KEY):
            for frame in list(tags.getall("TXXX")):
                if frame.desc.upper() == key:
                    tags.delall(f"TXXX:{frame.desc}")
        tags.add(TXXX(encoding=3, desc=TRACK_KEY, text=track_uuid))
        tags.add(TXXX(encoding=3, desc=ALBUM_KEY, text=album_uuid))
        # Keep whatever ID3 version the file already uses. Saving without this
        # silently upgrades v2.3 to v2.4, which changes how date frames are
        # represented - a library-wide format change nobody asked for.
        existing_version = getattr(tags, "version", (2, 4, 0))
        tags.save(path, v2_version=3 if existing_version[1] == 3 else 4)
        return

    if suffix in (".flac", ".ogg", ".oga", ".opus"):
        audio = MutagenFile(path)
        if audio is None:
            raise TagError("unreadable")
        if audio.tags is None:
            audio.add_tags()
        audio.tags[TRACK_KEY] = track_uuid
        audio.tags[ALBUM_KEY] = album_uuid
        audio.save()
        return

    if suffix in (".m4a", ".mp4"):
        audio = MP4(path)
        if audio.tags is None:
            audio.add_tags()
        audio.tags[MP4_TRACK] = [track_uuid.encode("utf-8")]
        audio.tags[MP4_ALBUM] = [album_uuid.encode("utf-8")]
        audio.save()
        return

    raise TagError(f"unsupported format: {suffix}")


# --- album grouping --------------------------------------------------------

def _album_uuid_for(directory: Path, cache: dict[Path, str]) -> str:
    """The album UUID for a directory, inherited from siblings when possible.

    Reading siblings matters: a track added to an album months later must join
    the album that is already there rather than founding a second one.
    """
    if directory in cache:
        return cache[directory]

    for sibling in sorted(directory.iterdir()):
        if sibling.is_file() and sibling.suffix.lower() in AUDIO_SUFFIXES:
            try:
                _, existing = _read(sibling)
            except Exception:
                continue
            if existing:
                cache[directory] = existing
                return existing

    cache[directory] = str(uuid.uuid4())
    return cache[directory]


# --- main ------------------------------------------------------------------

def process(path: Path, cache: dict[Path, str], apply: bool) -> dict:
    """Ensure both tags exist on one file. Returns a result record."""
    result = {"path": str(path), "action": None, "track_uuid": None,
              "album_uuid": None, "error": None}
    try:
        track_uuid, album_uuid = _read(path)
        needed_album = album_uuid or _album_uuid_for(path.parent, cache)

        if track_uuid and album_uuid:
            result.update(action="present", track_uuid=track_uuid,
                          album_uuid=album_uuid)
            return result

        new_track = track_uuid or str(uuid.uuid4())
        result.update(track_uuid=new_track, album_uuid=needed_album,
                      action="would-write" if not apply else "written")
        if not apply:
            return result

        # Restoring mtime keeps a scanner from treating every file in the
        # library as changed. Nothing here alters audio, so the file genuinely
        # is the same recording it was a moment ago.
        stat = path.stat()
        _write(path, new_track, needed_album)
        import os
        os.utime(path, (stat.st_atime, stat.st_mtime))

        # Mandatory checkpoint: a write that did not survive the round trip is
        # a failure, not a success, however plausible it looked.
        verify_track, verify_album = _read(path)
        if verify_track != new_track or verify_album != needed_album:
            raise TagError(
                f"verification failed (got {verify_track!r}/{verify_album!r})"
            )
        cache.setdefault(path.parent, needed_album)

    except Exception as exc:
        result.update(action="error", error=f"{type(exc).__name__}: {exc}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", type=Path,
                        help="directories to walk")
    parser.add_argument("--apply", action="store_true",
                        help="actually write tags (default is a dry run)")
    parser.add_argument("--log", type=Path,
                        help="append a JSON lines record of every assignment")
    parser.add_argument("--exclude", action="append", default=[],
                        help="skip paths containing this substring")
    args = parser.parse_args()

    files: list[Path] = []
    for root in args.roots:
        if not root.exists():
            print(f"!! no such path: {root}", file=sys.stderr)
            continue
        for candidate in sorted(root.rglob("*")):
            if not candidate.is_file():
                continue
            if candidate.suffix.lower() not in AUDIO_SUFFIXES:
                continue
            if any(pattern in str(candidate) for pattern in args.exclude):
                continue
            files.append(candidate)

    mode = "APPLYING" if args.apply else "DRY RUN (nothing will be written)"
    print(f"{mode} - {len(files)} audio files\n")

    cache: dict[Path, str] = {}
    counts = {"present": 0, "written": 0, "would-write": 0, "error": 0}
    errors: list[dict] = []
    handle = args.log.open("a", encoding="utf-8") if args.log else None

    try:
        for index, path in enumerate(files, 1):
            record = process(path, cache, args.apply)
            counts[record["action"]] = counts.get(record["action"], 0) + 1
            if record["action"] == "error":
                errors.append(record)
            if handle and record["action"] in ("written", "present"):
                record["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                handle.write(json.dumps(record) + "\n")
            if index % 250 == 0 or index == len(files):
                # flush explicitly: stdout is block-buffered when redirected to
                # a file, so progress on a long run would otherwise be invisible
                # until the buffer filled or the process ended.
                print(f"  {index}/{len(files)}  "
                      + "  ".join(f"{k}={v}" for k, v in counts.items() if v),
                      flush=True)
                if handle:
                    handle.flush()
    finally:
        if handle:
            handle.close()

    print("\n" + "-" * 56)
    for key, value in counts.items():
        if value:
            print(f"  {key:<12} {value}")
    print(f"  {'albums':<12} {len(cache)}")

    if errors:
        print(f"\n{len(errors)} FAILURES (nothing else was affected):")
        for record in errors[:20]:
            print(f"  {record['path']}\n     {record['error']}")
        if len(errors) > 20:
            print(f"  ... and {len(errors) - 20} more")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
