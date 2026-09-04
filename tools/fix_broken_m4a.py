#!/usr/bin/env python3
"""Repair .m4a files that are not actually MP4 containers.

Files downloaded as raw AAC streams often land with an .m4a extension without
ever being wrapped in an MP4 container. They play fine, because decoders sniff
the content, but they cannot carry tags - there is no container to put tags in.

Two ways out:

  --mode remux      wrap the existing audio stream in a real MP4 container.
                    A stream copy: the audio is bit-identical, nothing is
                    re-encoded, and the result is taggable. Preferred.

  --mode mp3        transcode to MP3 320.
  --mode flac       transcode to FLAC. Note this cannot recover anything the
                    original lossy encode discarded; it stores the same audio
                    in roughly three times the space.

Both transcode modes cost a second generation of lossy encoding on audio that
has already been through one. Add --all to convert every .m4a rather than only
the unreadable ones, for a library that should not hold the format at all.

Originals are never deleted. They are moved to a quarantine directory only
after the replacement has been verified as decodable, tag-capable, and the
same duration.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from mutagen import File as MutagenFile
    from mutagen.mp4 import MP4
except ImportError:
    sys.exit("mutagen is required:  pip3 install --user mutagen")

# Durations may differ very slightly across container formats; more than this
# means something was actually lost.
DURATION_TOLERANCE = 1.0


def probe(path: Path) -> dict | None:
    """Return ffprobe's view of a file, or None if it cannot be read."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def duration_of(info: dict | None) -> float | None:
    if not info:
        return None
    try:
        return float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        return None


def audio_codec(info: dict | None) -> str | None:
    for stream in (info or {}).get("streams", []):
        if stream.get("codec_type") == "audio":
            return stream.get("codec_name")
    return None


def find_targets(roots: list[Path], every: bool) -> list[Path]:
    """The .m4a files to act on.

    By default only the ones that are not real MP4 containers, since those are
    the ones that cannot hold tags. With `every`, all of them - for a library
    that should not contain the format at all.
    """
    targets = []
    for root in roots:
        for path in sorted(root.rglob("*.m4a")):
            if every:
                targets.append(path)
                continue
            try:
                MP4(path)
            except Exception:
                targets.append(path)
    return targets


def convert(source: Path, mode: str) -> tuple[Path | None, str]:
    """Produce a repaired file beside the original. Returns (path, note)."""
    if mode == "remux":
        target = source.with_suffix(".fixed.m4a")
        command = ["ffmpeg", "-v", "error", "-y", "-i", str(source),
                   "-map", "0:a", "-c:a", "copy",
                   "-map_metadata", "0", "-movflags", "+faststart", str(target)]
        note = "stream copy, no re-encode"
    elif mode == "flac":
        target = source.with_suffix(".fixed.flac")
        command = ["ffmpeg", "-v", "error", "-y", "-i", str(source),
                   "-map", "0:a", "-c:a", "flac", "-map_metadata", "0", str(target)]
        note = "transcoded to flac"
    else:
        target = source.with_suffix(".fixed.mp3")
        command = ["ffmpeg", "-v", "error", "-y", "-i", str(source),
                   "-map", "0:a", "-c:a", "libmp3lame", "-b:a", "320k",
                   "-map_metadata", "0", "-id3v2_version", "3", str(target)]
        note = "transcoded to mp3 320"

    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0 or not target.exists():
        target.unlink(missing_ok=True)
        return None, (result.stderr or "ffmpeg failed").strip()[:160]
    return target, note


def verify(original: Path, repaired: Path, source_duration: float | None) -> str | None:
    """Return an error string if the repaired file is not acceptable."""
    info = probe(repaired)
    if info is None:
        return "repaired file is not readable"

    new_duration = duration_of(info)
    if source_duration and new_duration:
        drift = abs(new_duration - source_duration)
        if drift > DURATION_TOLERANCE:
            return f"duration changed by {drift:.1f}s"

    # The entire point is to end up with something that can hold tags.
    try:
        audio = MutagenFile(repaired)
        if audio is None:
            return "mutagen cannot read the repaired file"
        if audio.tags is None:
            audio.add_tags()
            audio.save()
    except Exception as exc:
        return f"repaired file is not taggable: {type(exc).__name__}: {exc}"

    if repaired.stat().st_size == 0:
        return "repaired file is empty"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", type=Path)
    parser.add_argument("--mode", choices=("remux", "mp3", "flac"), default="remux")
    parser.add_argument("--all", action="store_true", dest="every",
                        help="convert every .m4a, not only the unreadable ones")
    parser.add_argument("--apply", action="store_true",
                        help="actually convert (default is a dry run)")
    parser.add_argument("--quarantine", type=Path, default=Path.home() / "m4a-originals",
                        help="where originals are moved after a verified repair")
    args = parser.parse_args()

    if args.every and args.mode == "remux":
        sys.exit("--all with --mode remux would rewrite .m4a as .m4a; "
                 "use --mode mp3 or --mode flac")

    broken = find_targets([r for r in args.roots if r.exists()], args.every)
    scope = "all" if args.every else "broken"
    mode_text = "APPLYING" if args.apply else "DRY RUN (nothing will change)"
    print(f"{mode_text} - {len(broken)} {scope} .m4a files, mode={args.mode}\n")
    if not broken:
        return 0

    fixed = failed = 0
    problems: list[str] = []

    for path in broken:
        info = probe(path)
        codec = audio_codec(info)
        source_duration = duration_of(info)

        if info is None:
            problems.append(f"{path}\n     unreadable by ffmpeg - genuinely corrupt")
            failed += 1
            continue

        label = f"{codec or '?'} {source_duration or 0:.0f}s"
        if not args.apply:
            print(f"  would fix  {label:<14} {path.name[:58]}")
            continue

        repaired, note = convert(path, args.mode)
        if repaired is None:
            problems.append(f"{path}\n     {note}")
            failed += 1
            continue

        error = verify(path, repaired, source_duration)
        if error:
            repaired.unlink(missing_ok=True)
            problems.append(f"{path}\n     {error}")
            failed += 1
            continue

        # Only now is it safe to displace the original.
        args.quarantine.mkdir(parents=True, exist_ok=True)
        keep = args.quarantine / path.name
        for n in range(2, 200):
            if not keep.exists():
                break
            keep = args.quarantine / f"{path.stem} ({n}){path.suffix}"

        final = path.with_suffix({"remux": ".m4a", "mp3": ".mp3",
                                  "flac": ".flac"}[args.mode])
        shutil.move(str(path), str(keep))
        shutil.move(str(repaired), str(final))
        print(f"  fixed      {label:<14} {final.name[:58]}")
        fixed += 1

    print("\n" + "-" * 56)
    if args.apply:
        print(f"  repaired  {fixed}")
        print(f"  failed    {failed}")
        print(f"  originals kept in {args.quarantine}")
    else:
        print(f"  {len(broken)} files would be repaired")

    if problems:
        print(f"\n{len(problems)} could not be repaired:")
        for line in problems:
            print(f"  {line}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
