#!/usr/bin/env python3
"""Identify library files by acoustic fingerprint and write back the ids.

For files whose tags are wrong or absent, which no amount of tag matching can
rescue. It asks what the audio *sounds like* instead: fpcalc computes a
chromaprint, AcoustID maps that to a MusicBrainz recording, and the recording
id is written onto the file. From there `beet update` and `beet mbsync` can
fill in everything else from MusicBrainz.

Nothing is moved and nothing is retagged beyond the two identifier fields, so
a starred track stays exactly where it is and keeps its star. That is the
whole point of doing it this way rather than by re-importing: 25 of the files
this was written for are starred and 67 have play counts.

    python -m tools.fingerprint /music --api-key KEY
    python -m tools.fingerprint /music --api-key KEY --apply

A free AcoustID key comes from https://acoustid.org/new-application - this
deliberately does not ship one, because a key identifies the application
making the request and borrowing another project's is not ours to do.

RESOURCE SAFETY
---------------
Fingerprinting a whole library is the kind of job that makes a Raspberry Pi
unusable for everything else. This is built not to:

  * one file at a time, never in parallel
  * the process renices itself to the lowest priority
  * it watches /proc/loadavg and waits when the machine is already busy
  * AcoustID is called at most once a second, well inside their rate limit
  * progress is checkpointed, so an interrupted run resumes instead of
    starting the whole library again

Even so, run it under a hard CPU ceiling. Measured on this Pi, `--cpus=1.0`
holds a four-core machine to one core (11.92 cpu-seconds unbounded against
3.05 bounded over the same three seconds):

    docker run --rm --cpus=1.0 -v ...:/music -v ...:/config \\
        --entrypoint python ghcr.io/awdimartino/download_center:latest \\
        -m tools.fingerprint /music --api-key KEY
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("requests is required")

from mutagen.id3 import ID3, TXXX, UFID
from mutagen.id3._util import ID3NoHeaderError

AUDIO_SUFFIXES = {".mp3"}

ACOUSTID_URL = "https://api.acoustid.org/v2/lookup"

# AcoustID asks for no more than three requests a second. One is plenty for a
# background backfill and leaves their service alone.
SECONDS_BETWEEN_LOOKUPS = 1.0

# Wait rather than compete when the machine is already loaded. Expressed per
# core, so it means the same thing on a Pi as on anything larger.
LOAD_CEILING_PER_CORE = 1.5
LOAD_CHECK_SECONDS = 15

FPCALC_TIMEOUT = 60
LOOKUP_TIMEOUT = 20


def _cores() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 1


def wait_for_quiet(ceiling: float, quiet: bool = False) -> None:
    """Block while the machine is busy with something else.

    A backfill has no deadline. Anything else on this box does - somebody is
    listening to music through it - so this yields rather than competes.
    """
    cores = _cores()
    while True:
        try:
            load = os.getloadavg()[0]
        except OSError:
            return
        if load <= ceiling * cores:
            return
        if not quiet:
            print(f"  load {load:.2f} over {ceiling * cores:.2f}, waiting",
                  flush=True)
        time.sleep(LOAD_CHECK_SECONDS)


def fingerprint(path: Path) -> tuple[int, str] | None:
    """(duration, fingerprint) from fpcalc, or None if it could not read it."""
    try:
        result = subprocess.run(
            ["fpcalc", "-json", str(path)],
            capture_output=True, text=True, timeout=FPCALC_TIMEOUT, check=False,
        )
    except FileNotFoundError:
        sys.exit("fpcalc is not installed (apt install libchromaprint-tools)")
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
        return int(data["duration"]), data["fingerprint"]
    except (ValueError, KeyError):
        return None


def lookup(api_key: str, duration: int, fp: str) -> dict | None:
    """Best AcoustID match, as {recording_id, title, artist, score}.

    Only the top result, and only when AcoustID is confident. A wrong
    identifier written onto a file is worse than no identifier: it is a
    confident lie that `mbsync` would then expand into a full set of wrong
    tags.
    """
    try:
        response = requests.post(
            ACOUSTID_URL,
            data={"client": api_key, "duration": duration, "fingerprint": fp,
                  "meta": "recordings", "format": "json"},
            timeout=LOOKUP_TIMEOUT,
            headers={"User-Agent": "download-center-fingerprint"},
        )
    except requests.RequestException as exc:
        print(f"  ! lookup failed: {str(exc)[:80]}", flush=True)
        return None
    if response.status_code != 200:
        print(f"  ! AcoustID returned {response.status_code}", flush=True)
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    if payload.get("status") != "ok":
        return None

    best = None
    for result in payload.get("results", []):
        score = result.get("score", 0)
        for recording in result.get("recordings", []) or []:
            if not recording.get("id"):
                continue
            if best is None or score > best["score"]:
                artists = recording.get("artists") or []
                best = {
                    "recording_id": recording["id"],
                    "title": recording.get("title", ""),
                    "artist": ", ".join(a.get("name", "") for a in artists),
                    "score": score,
                }
    return best


def write_ids(path: Path, recording_id: str) -> None:
    """Write the recording id, and nothing else.

    mtime is restored: these files are already indexed, and making the whole
    library look changed to a scanner is how an afternoon becomes a full
    rescan. Every other frame on the file is left alone, which is what keeps
    NAVIDROME_UUID - and therefore the stars hanging off it - intact.
    """
    stat = path.stat()
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    # Only our own two fields are replaced. An earlier draft also cleared
    # `MusicBrainz Release Track Id` on the theory that it would be stale,
    # which is a guess - and deleting metadata nobody asked to delete is not
    # something a backfill should do quietly.
    for frame in list(tags.getall("UFID")):
        if frame.owner == "http://musicbrainz.org":
            tags.delall(f"UFID:{frame.owner}")
    tags.add(UFID(owner="http://musicbrainz.org",
                  data=recording_id.encode("ascii")))
    tags.add(TXXX(encoding=3, desc="Acoustid Id", text=recording_id))
    tags.save(path, v2_version=4)
    os.utime(path, (stat.st_atime, stat.st_mtime))


def already_identified(path: Path) -> bool:
    try:
        tags = ID3(path)
    except Exception:
        return False
    return any(f.owner == "http://musicbrainz.org" for f in tags.getall("UFID"))


def load_done(checkpoint: Path) -> set[str]:
    if not checkpoint.exists():
        return set()
    return {line.split("\t")[0] for line in
            checkpoint.read_text(encoding="utf-8").splitlines() if line}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", type=Path)
    parser.add_argument("--api-key", default=None,
                        help="AcoustID key; defaults to the configured one")
    parser.add_argument("--apply", action="store_true",
                        help="write ids (default is a dry run)")
    parser.add_argument("--limit", type=int, default=0,
                        help="stop after this many files")
    parser.add_argument("--from-list", type=Path,
                        help="a file of paths, one per line, instead of walking")
    parser.add_argument("--checkpoint", type=Path,
                        default=Path("/config/fingerprint-progress.tsv"))
    parser.add_argument("--load-ceiling", type=float,
                        default=LOAD_CEILING_PER_CORE,
                        help="pause while load per core is above this")
    parser.add_argument("--min-score", type=float, default=0.85,
                        help="reject AcoustID matches below this confidence")
    args = parser.parse_args()

    # Settings first, so the key is set once in the Settings panel rather than
    # pasted onto a command line every time - and so it is not sitting in the
    # shell history of whoever last ran a backfill.
    api_key = args.api_key
    if not api_key:
        try:
            from app.config import settings
            api_key = settings.acoustid_key
        except Exception:
            api_key = os.environ.get("DC_ACOUSTID_KEY", "")
    if not api_key:
        sys.exit("No AcoustID key. Set one in Settings (or pass --api-key).\n"
                 "Free, from https://acoustid.org/new-application\n"
                 "\n"
                 "Note this is only needed for this backfill. Ordinary imports\n"
                 "are fingerprinted by the beets chroma plugin, which carries\n"
                 "its own key and needs nothing configured.")

    # Lowest priority. The machine is serving music; this is housekeeping.
    try:
        os.nice(19)
    except (AttributeError, OSError):
        pass

    if args.from_list:
        files = [Path(line.strip()) for line in
                 args.from_list.read_text(encoding="utf-8").splitlines()
                 if line.strip()]
    else:
        files = []
        for root in args.roots:
            files.extend(sorted(p for p in root.rglob("*")
                                if p.is_file()
                                and p.suffix.lower() in AUDIO_SUFFIXES))

    done = load_done(args.checkpoint)
    pending = [f for f in files if str(f) not in done]
    if args.limit:
        pending = pending[:args.limit]

    print(f"{'APPLYING' if args.apply else 'DRY RUN - nothing will be written'}")
    print(f"  files in scope : {len(files)}")
    print(f"  already done   : {len(files) - len([f for f in files if str(f) not in done])}")
    print(f"  to process now : {len(pending)}")
    print(f"  cpu cores seen : {_cores()}  (pausing above load "
          f"{args.load_ceiling * _cores():.1f})")
    print(flush=True)

    identified = skipped = failed = 0
    started = time.time()
    handle = args.checkpoint.open("a", encoding="utf-8") if args.apply else None

    try:
        for index, path in enumerate(pending, 1):
            wait_for_quiet(args.load_ceiling)

            if already_identified(path):
                skipped += 1
                continue

            got = fingerprint(path)
            if got is None:
                failed += 1
                print(f"  ? unreadable  {path.name[:60]}", flush=True)
                continue
            duration, fp = got

            match = lookup(api_key, duration, fp)
            time.sleep(SECONDS_BETWEEN_LOOKUPS)

            if not match or match["score"] < args.min_score:
                failed += 1
                score = f"{match['score']:.2f}" if match else "none"
                print(f"  - no match ({score})  {path.name[:52]}", flush=True)
                continue

            identified += 1
            print(f"  + {match['score']:.2f}  {match['artist'][:24]} - "
                  f"{match['title'][:30]}   <- {path.name[:36]}", flush=True)
            if args.apply:
                try:
                    write_ids(path, match["recording_id"])
                except Exception as exc:
                    failed += 1
                    identified -= 1
                    print(f"  ! write failed: {exc}", flush=True)
                    continue
                handle.write(f"{path}\t{match['recording_id']}\n")
                handle.flush()

            if index % 25 == 0:
                rate = index / max(time.time() - started, 1)
                left = (len(pending) - index) / max(rate, 0.001)
                print(f"    .. {index}/{len(pending)}  "
                      f"{identified} identified, {failed} no match, "
                      f"~{left / 60:.0f} min left", flush=True)
    except KeyboardInterrupt:
        print("\ninterrupted - rerun to resume from the checkpoint")
    finally:
        if handle:
            handle.close()

    print()
    print(f"  identified     : {identified}")
    print(f"  already had id : {skipped}")
    print(f"  no match       : {failed}")
    if not args.apply:
        print("\nNothing was written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
