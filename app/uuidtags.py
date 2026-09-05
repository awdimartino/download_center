"""Reading the identity tags Navidrome derives its persistent IDs from.

Navidrome computes a track's identity from a configurable tag formula. Pointed
at a self-generated UUID, identity stops depending on match quality or on the
path, and becomes one binary fact: the tag is present and unchanged, or it is
not. Stars, ratings and play counts then survive any retag or reorganisation.

The tag lives in a different place in every container format, which is the
only reason this module exists.

`tools/ensure_uuid.py` deliberately duplicates the write side. It runs on a
host with nothing installed but mutagen, so it stays a standalone script.
"""

from __future__ import annotations

from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.id3 import ID3, ID3NoHeaderError, TXXX
from mutagen.mp4 import MP4

TRACK_KEY = "NAVIDROME_UUID"
ALBUM_KEY = "NAVIDROME_ALBUM_UUID"

# MP4 has no free-text tag space, so freeform atoms carry a "mean" namespace.
# Navidrome reads these through TagLib, which surfaces freeform atoms under
# com.apple.iTunes - the de facto namespace every player understands. Atoms
# written under any other mean parse fine with mutagen and are invisible to
# Navidrome, which is exactly what happened here: it read the standard atoms
# from these files and none of ours.
MP4_TRACK = "----:com.apple.iTunes:NAVIDROME_UUID"
MP4_ALBUM = "----:com.apple.iTunes:NAVIDROME_ALBUM_UUID"

# What earlier versions wrote. Still read so an already-stamped file keeps the
# identity it has - regenerating one would orphan whatever it carries.
MP4_TRACK_LEGACY = "----:com.navidrome:UUID"
MP4_ALBUM_LEGACY = "----:com.navidrome:ALBUM_UUID"

AUDIO_SUFFIXES = {".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".oga", ".opus",
                  ".wav", ".wv", ".aiff", ".ape"}

# Formats that cannot carry the tags at all. Reporting these as "missing a
# UUID" would be noise: nothing can be done short of converting the file.
UNTAGGABLE_SUFFIXES = {".wav", ".aiff"}


class UnreadableFile(Exception):
    """The file exists but its tags could not be parsed."""


def read(path: Path) -> tuple[str | None, str | None]:
    """Return (track_uuid, album_uuid), either of which may be absent."""
    suffix = path.suffix.lower()

    if suffix == ".mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            # Carrying no ID3 header is a legitimate state for a real MP3 and
            # a certainty for a file that is not one. The parser cannot tell
            # them apart, so fall back to sniffing the audio itself: an
            # untagged track is missing a UUID, a corrupt file is a different
            # problem with a different fix.
            try:
                sniffed = MutagenFile(path)
            except Exception as exc:
                raise UnreadableFile(f"not a readable MP3: {exc}"[:120]) from None
            if sniffed is None:
                raise UnreadableFile("not a readable MP3") from None
            return None, None
        except Exception as exc:
            raise UnreadableFile(f"{type(exc).__name__}: {exc}") from exc
        found: dict[str, str] = {}
        for frame in tags.getall("TXXX"):
            key = frame.desc.upper()
            if key in (TRACK_KEY, ALBUM_KEY) and frame.text:
                found[key] = str(frame.text[0])
        return found.get(TRACK_KEY), found.get(ALBUM_KEY)

    if suffix in (".m4a", ".mp4"):
        try:
            audio = MP4(path)
        except Exception as exc:
            raise UnreadableFile(f"{type(exc).__name__}: {exc}") from exc

        def atom(*names: str) -> str | None:
            for name in names:
                values = audio.tags.get(name) if audio.tags else None
                if not values:
                    continue
                raw = values[0]
                return (raw.decode("utf-8", "replace")
                        if isinstance(raw, bytes) else str(raw))
            return None

        return (atom(MP4_TRACK, MP4_TRACK_LEGACY),
                atom(MP4_ALBUM, MP4_ALBUM_LEGACY))

    try:
        audio = MutagenFile(path)
    except Exception as exc:
        raise UnreadableFile(f"{type(exc).__name__}: {exc}") from exc
    if audio is None:
        raise UnreadableFile("unrecognised format")
    if audio.tags is None:
        return None, None

    def comment(key: str) -> str | None:
        # Vorbis comment keys are case-insensitive by spec but stored as
        # written, so both spellings have to be tried.
        values = audio.tags.get(key) or audio.tags.get(key.lower())
        return str(values[0]) if values else None

    return comment(TRACK_KEY), comment(ALBUM_KEY)


def album_name(path: Path) -> str:
    """The album tag, used to decide what counts as one album on disk."""
    try:
        audio = MutagenFile(path, easy=True)
    except Exception:
        return ""
    if audio is None or not audio.tags:
        return ""
    values = audio.tags.get("album")
    return str(values[0]).strip() if values else ""


def write(path: Path, track_uuid: str | None, album_uuid: str | None) -> None:
    """Set either tag, leaving the rest of the file's tags alone.

    The caller is responsible for mtime: restoring it keeps a scanner from
    treating the whole library as changed, but also means an incremental scan
    will not notice the new tags.
    """
    suffix = path.suffix.lower()
    pairs = [(TRACK_KEY, track_uuid), (ALBUM_KEY, album_uuid)]

    if suffix == ".mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        for key, value in pairs:
            if value is None:
                continue
            # Remove only our own frames; TXXX descriptions are free text and
            # anything else in there belongs to some other tool.
            for frame in list(tags.getall("TXXX")):
                if frame.desc.upper() == key:
                    tags.delall(f"TXXX:{frame.desc}")
            tags.add(TXXX(encoding=3, desc=key, text=value))
        # Keep whatever ID3 version the file already uses. Saving without
        # this silently promotes v2.3 to v2.4, which changes how date frames
        # are written - a library-wide format change nobody asked for.
        version = getattr(tags, "version", (2, 4, 0))
        tags.save(path, v2_version=3 if version[1] == 3 else 4)
        return

    if suffix in (".m4a", ".mp4"):
        audio = MP4(path)
        if audio.tags is None:
            audio.add_tags()
        for atom, legacy, value in ((MP4_TRACK, MP4_TRACK_LEGACY, track_uuid),
                                    (MP4_ALBUM, MP4_ALBUM_LEGACY, album_uuid)):
            if value is None:
                continue
            audio.tags[atom] = [value.encode("utf-8")]
            # Drop the old atom so a file cannot end up carrying two answers.
            audio.tags.pop(legacy, None)
        audio.save()
        return

    audio = MutagenFile(path)
    if audio is None:
        raise UnreadableFile("unrecognised format")
    if audio.tags is None:
        audio.add_tags()
    for key, value in pairs:
        if value is not None:
            audio.tags[key] = value
    audio.save()


def is_audio(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_SUFFIXES


def can_carry_tags(path: Path) -> bool:
    return path.suffix.lower() not in UNTAGGABLE_SUFFIXES
