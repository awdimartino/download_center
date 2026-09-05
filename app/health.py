"""Library health checks, read from Navidrome's database and the filesystem.

Everything this module reports is something that was invisible until someone
went looking for it. A path template referencing a field that does not exist,
albums split across folders, files carrying no persistent identity - none of
it announces itself, and by the time it surfaces the damage is months deep.

So the panel is deliberately shaped as a list of numbers that should be zero.
Anything non-zero is a thing to look at; anything zero is silence.

Navidrome's database is opened **read-only**. Navidrome owns that file and
caches from it, and a second writer risks lock contention and inconsistent
state that no amount of care here would prevent. Anything that needs to change
state goes through Navidrome's HTTP API instead.
"""

from __future__ import annotations

import contextlib
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import diskaudit, navidrome, uuidtags
from .config import settings

# The tag whose value Navidrome is configured to use as its persistent track
# identity. Stored parsed in media_file.tags, so it can be queried directly
# rather than by reading several thousand files.
UUID_TAG = "$.navidrome_uuid[0].value"
ALBUM_UUID_TAG = "$.navidrome_album_uuid[0].value"

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"

# Formats with nowhere to put a custom tag, quoted for an IN clause. Kept in
# step with uuidtags.UNTAGGABLE_SUFFIXES; Navidrome stores the suffix without
# the leading dot.
_UNTAGGABLE_SQL = ", ".join(
    f"'{suffix.lstrip('.')}'" for suffix in sorted(uuidtags.UNTAGGABLE_SUFFIXES)
)


@dataclass
class Check:
    """One reported number, and whether it is a problem."""

    key: str
    label: str
    value: Any
    status: str = OK
    detail: str = ""
    # Where to look when the number is not what it should be.
    hint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label, "value": self.value,
            "status": self.status, "detail": self.detail, "hint": self.hint,
        }


@dataclass
class Section:
    title: str
    checks: list[Check] = field(default_factory=list)

    def add(self, *checks: Check) -> None:
        self.checks.extend(checks)

    def as_dict(self) -> dict[str, Any]:
        return {"title": self.title,
                "checks": [check.as_dict() for check in self.checks]}


# Kept as an alias so callers reading "health" still name the right error.
NavidromeUnavailable = navidrome.Unavailable
_connect = navidrome.open_db


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"pragma table_info({table})")}


def _scalar(connection: sqlite3.Connection, sql: str, *args) -> int:
    row = connection.execute(sql, args).fetchone()
    return (row[0] or 0) if row else 0


def _live_clause(connection: sqlite3.Connection,
                 libraries: list[int] | None = None) -> str:
    """What counts as a track that actually exists.

    `media_file.missing` alone is not enough. When a whole directory
    disappears Navidrome marks the *folder* missing and leaves the rows
    beneath it untouched, so filtering on the file flag alone counts tracks
    that vanished months ago - and reports them as unstamped, which sends you
    looking for files that are not there.
    """
    columns = _columns(connection, "media_file")
    clause = "1=1" if "missing" not in columns else "mf.missing = 0"
    if "missing" in columns and "folder_id" in columns and _columns(connection, "folder"):
        clause += (" and mf.folder_id in "
                   "(select id from folder where missing = 0)")
    # Somebody else's collection is not this person's problem to see.
    if libraries is not None:
        inside = ",".join(str(int(i)) for i in libraries) or "-1"
        clause += f" and mf.library_id in ({inside})"
    return clause


# --- the checks -----------------------------------------------------------

def _identity_section(connection: sqlite3.Connection, live: str) -> Section:
    """Whether every track still has a stable identity.

    This is the load-bearing one. A track without the UUID tag falls back to
    an identity derived from album/disc/track/title, which changes the moment
    anything retags the file - taking its stars and play count with it.
    """
    section = Section("Identity")

    total = _scalar(connection, f"select count(*) from media_file mf where {live}")
    stamped = _scalar(connection, f"""
        select count(*) from media_file mf
         where {live} and json_extract(mf.tags, '{UUID_TAG}') is not null""")

    # A wav has nowhere to put the tag. Counting those as failures leaves a
    # red number that can never reach zero, which is how a panel like this
    # trains you to stop reading it.
    untaggable = _scalar(connection, f"""
        select count(*) from media_file mf
         where {live} and json_extract(mf.tags, '{UUID_TAG}') is null
           and lower(mf.suffix) in ({_UNTAGGABLE_SQL})""")

    unstamped = total - stamped - untaggable
    section.add(Check(
        "unstamped", "Tracks with no UUID", unstamped,
        OK if unstamped == 0 else FAIL,
        f"{stamped} of {total - untaggable} taggable tracks carry one"
        + (f", {untaggable} cannot hold tags" if untaggable else ""),
        "Run the stamper, then a full scan - stamping preserves mtime, so an "
        "incremental scan will not notice the new tags.",
    ))

    orphans = _scalar(connection, """
        select count(*) from annotation
         where item_type = 'media_file'
           and (starred = 1 or rating > 0)
           and item_id not in (select id from media_file)""")
    section.add(Check(
        "orphan_annotations", "Stars and ratings pointing nowhere", orphans,
        OK if orphans == 0 else WARN,
        "annotation rows whose track no longer exists",
        "Usually the aftermath of purging missing files. Safe to delete.",
    ))

    # Two tracks sharing one UUID means the tag was copied rather than
    # generated - they would collapse into a single track in Navidrome.
    collisions = _scalar(connection, f"""
        select count(*) from (
            select json_extract(mf.tags, '{UUID_TAG}') as u
              from media_file mf where {live}
               and json_extract(mf.tags, '{UUID_TAG}') is not null
             group by u having count(*) > 1)""")
    section.add(Check(
        "uuid_collisions", "Duplicate UUIDs", collisions,
        OK if collisions == 0 else FAIL,
        "distinct UUIDs claimed by more than one file",
    ))

    return section


def _library_section(connection: sqlite3.Connection, live: str,
                     visible: list[dict[str, Any]]) -> Section:
    """Per-library totals.

    Reported separately because aggregates hide things: one library being
    fully stamped and another not at all averages out to something that looks
    like a partial failure of both.
    """
    section = Section("Libraries")
    columns = _columns(connection, "library")

    for row in visible:
        total = _scalar(
            connection,
            f"select count(*) from media_file mf where {live} and library_id = ?",
            row["id"])
        stamped = _scalar(connection, f"""
            select count(*) from media_file mf
             where {live} and library_id = ?
               and json_extract(mf.tags, '{UUID_TAG}') is not null""", row["id"])
        percent = round(100 * stamped / total) if total else 100
        section.add(Check(
            f"library_{row['id']}", row["name"], total,
            OK if percent == 100 else WARN,
            f"{percent}% stamped",
        ))

    if "missing" in _columns(connection, "media_file"):
        # The inverse of "live" rather than the file's own flag, because a
        # vanished directory is recorded on the folder and leaves the rows
        # beneath it looking present. Bounded to these libraries: the inverse
        # of a scoped clause otherwise counts every file somebody else owns
        # as one of yours that went missing.
        inside = ",".join(str(int(lib["id"])) for lib in visible) or "-1"
        held = _scalar(connection, "select count(*) from media_file mf"
                                   f" where mf.library_id in ({inside})")
        present = _scalar(connection,
                          f"select count(*) from media_file mf where {live}")
        missing = held - present
        section.add(Check(
            "missing_files", "Files Navidrome can no longer find", missing,
            OK if missing == 0 else INFO,
            "rows kept so their stars survive if the file returns",
        ))

    if "last_scan_at" in columns:
        row = connection.execute(
            "select max(last_scan_at) from library").fetchone()
        section.add(Check("last_scan", "Last scan", row[0] or "never", INFO))

    return section


def _metadata_section(connection: sqlite3.Connection, live: str) -> Section:
    """Tagging quality. None of this is dangerous, it is just untidy."""
    section = Section("Metadata")
    columns = _columns(connection, "media_file")

    unknown_album = _scalar(connection, f"""
        select count(*) from media_file mf
         where {live} and (album is null or album = ''
                           or album like '%Unknown Album%')""")
    section.add(Check(
        "no_album", "Tracks with no album", unknown_album,
        OK if unknown_album == 0 else INFO,
        hint="These land in the Unknown Album fallback folder.",
    ))

    no_track = _scalar(connection, f"""
        select count(*) from media_file mf
         where {live} and (track_number is null or track_number = 0)""")
    section.add(Check(
        "no_track_number", "Tracks numbered zero", no_track,
        OK if no_track == 0 else INFO,
        hint="Usually a single filed as though it were a whole album.",
    ))

    if "rg_track_gain" in columns:
        total = _scalar(connection,
                        f"select count(*) from media_file mf where {live}")
        gained = _scalar(connection, f"""
            select count(*) from media_file mf
             where {live} and rg_track_gain is not null and rg_track_gain != 0""")
        section.add(Check(
            "no_replaygain", "Tracks with no ReplayGain", total - gained,
            OK if total == gained else INFO,
            f"{gained} of {total} measured",
            "beet replaygain backfills these. Navidrome also needs ReplayGain "
            "mode set to Track in personal settings before it applies them.",
        ))

    if "mbz_recording_id" in columns:
        without = _scalar(connection, f"""
            select count(*) from media_file mf
             where {live} and (mbz_recording_id is null or mbz_recording_id = '')""")
        section.add(Check(
            "no_mbid", "Tracks with no MusicBrainz id", without, INFO,
            hint="A rough proxy for never having been matched. Overcounts "
                 "anything tagged by hand.",
        ))

    return section


def _staging_section() -> Section:
    """What is waiting for a human.

    The staging tree is the queue: beets moves out everything it can match, so
    whatever is left is by definition something it refused. There is no
    separate list of pending work to fall out of sync.
    """
    section = Section("Staging")

    def survey(directory: Path) -> tuple[int, float | None]:
        if not directory.exists():
            return 0, None
        entries = [p for p in directory.iterdir() if not p.name.startswith(".")]
        oldest = min((p.stat().st_mtime for p in entries), default=None)
        return len(entries), oldest

    albums, albums_oldest = survey(settings.albums_dir)
    singles, singles_oldest = survey(settings.singles_dir)

    section.add(Check(
        "staging_albums", "Albums awaiting attention", albums,
        OK if albums == 0 else WARN))
    section.add(Check(
        "staging_singles", "Singles awaiting attention", singles,
        OK if singles == 0 else WARN))

    oldest = min([t for t in (albums_oldest, singles_oldest) if t], default=None)
    if oldest:
        days = (time.time() - oldest) / 86400
        section.add(Check(
            "staging_age", "Oldest item", f"{days:.0f} days",
            OK if days < 7 else WARN,
            hint="Anything sitting here for a week is not going to import "
                 "itself.",
        ))

    return section


def _merge_audits(audits: list) -> Any:
    """Fold several library audits into the one figure a person sees.

    Someone with two libraries wants to know whether *their music* is sound,
    not to read the same five checks twice. Counts add; the age shown is the
    oldest, since a summary is only as current as its stalest part.
    """
    if not audits:
        return None
    if len(audits) == 1:
        return audits[0]

    merged = diskaudit.Audit()
    for audit in audits:
        merged.files += audit.files
        merged.stamped += audit.stamped
        merged.untaggable += audit.untaggable
        merged.seconds += audit.seconds
        for name in ("missing_track_uuid", "missing_album_uuid", "unreadable",
                     "split_albums", "spanning_albums", "duplicate_uuids"):
            getattr(merged, name).extend(getattr(audit, name))
    merged.taken_at = min(a.taken_at for a in audits)
    return merged


def _disk_section(audit) -> Section:
    """What the files themselves say, as opposed to what Navidrome believes."""
    section = Section("On disk")

    if audit is None:
        section.add(Check(
            "disk_pending", "Audit", "not run yet", INFO,
            "walks the library reading tags; runs in the background",
        ))
        return section

    age = (time.time() - audit.taken_at) / 3600
    section.add(Check(
        "disk_files", "Audio files", audit.files, INFO,
        f"read {age:.0f}h ago in {audit.seconds:.0f}s"
        + (f", {audit.untaggable} untaggable" if audit.untaggable else ""),
    ))
    section.add(Check(
        "disk_unstamped", "Files with no UUID", len(audit.missing_track_uuid),
        OK if not audit.missing_track_uuid else FAIL,
        "; ".join(audit.missing_track_uuid[:3]),
        "Run the stamper over the library, then a full scan.",
    ))
    section.add(Check(
        "disk_split_albums", "Directories with two album UUIDs",
        len(audit.split_albums),
        OK if not audit.split_albums else FAIL,
        "; ".join(audit.split_albums[:3]),
        "One directory is one album. Two UUIDs means Navidrome shows it "
        "twice, however tidy the folder is.",
    ))
    section.add(Check(
        "disk_spanning_albums", "Album UUIDs spread across directories",
        len(audit.spanning_albums),
        OK if not audit.spanning_albums else FAIL,
        "; ".join(audit.spanning_albums[:2]),
        "The reverse of a split album: unrelated tracks fused into one "
        "record. Happens when files are stamped together and filed apart "
        "afterwards. `stamp.resplit` gives each directory its own again.",
    ))
    section.add(Check(
        "disk_duplicate_uuids", "UUIDs on more than one file",
        len(audit.duplicate_uuids),
        OK if not audit.duplicate_uuids else FAIL,
        hint="A copied tag rather than a generated one. Both files collapse "
             "into a single track.",
    ))
    section.add(Check(
        "disk_unreadable", "Unreadable files", len(audit.unreadable),
        OK if not audit.unreadable else WARN,
        "; ".join(audit.unreadable[:2]),
    ))
    return section


def _stale_index_check(connection_stamped: int | None, audit) -> Check | None:
    """Whether Navidrome has caught up with the tags on disk.

    Stamping preserves mtime deliberately, so an incremental scan does not
    re-read the files and Navidrome keeps using the fallback identity. The
    database alone cannot distinguish that from files that were never
    stamped, and the two need opposite responses.
    """
    if audit is None or connection_stamped is None:
        return None
    behind = audit.stamped - connection_stamped
    if behind <= 0:
        return None
    return Check(
        "stale_index", "Stamped but not yet scanned", behind,
        WARN,
        "on disk but not in Navidrome's index",
        "Stamping preserves mtime, so incremental scans skip these. "
        "Run a full scan.",
    )


def _system_section(started_at: float) -> Section:
    section = Section("System")

    uptime = time.time() - started_at
    section.add(Check("uptime", "Uptime", _duration(uptime), INFO))

    target = settings.music_dir if settings.music_dir.exists() else Path(".")
    usage = shutil.disk_usage(target)
    free_fraction = usage.free / usage.total if usage.total else 1
    section.add(Check(
        "disk_free", "Free space",
        f"{usage.free / 1e9:.0f} GB",
        OK if free_fraction > 0.10 else (WARN if free_fraction > 0.05 else FAIL),
        f"{100 * free_fraction:.0f}% of {usage.total / 1e9:.0f} GB",
    ))
    return section


def _duration(seconds: float) -> str:
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def report(started_at: float,
           libraries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Everything the dashboard shows, for one person, in one pass.

    Scoped to the libraries they may see. An aggregate over somebody else's
    collection is not information they can act on, and reporting problems in
    it is how a panel of things-that-should-be-zero starts getting ignored.
    """
    sections: list[Section] = []
    error = None
    libraries = libraries or []
    roots = [Path(lib["path"]) for lib in libraries]
    audits = [a for a in (diskaudit.cached(r) for r in roots) if a]
    audit = _merge_audits(audits)
    indexed_stamped: int | None = None

    try:
        connection = _connect()
    except NavidromeUnavailable as exc:
        error = str(exc)
    else:
        with connection:
            live = _live_clause(connection, [lib["id"] for lib in libraries])
            # Navidrome's schema moves between releases, and json_extract
            # needs a SQLite built with JSON1. One section failing should
            # cost that section, not the whole panel.
            builders = (
                lambda c, l: _identity_section(c, l),
                lambda c, l: _library_section(c, l, libraries),
                lambda c, l: _metadata_section(c, l),
            )
            for build in builders:
                try:
                    sections.append(build(connection, live))
                except sqlite3.Error as exc:
                    sections.append(Section(
                        "Checks unavailable",
                        [Check("unavailable", "Query failed", "—",
                               INFO, str(exc)[:120])]))
            with contextlib.suppress(sqlite3.Error):
                indexed_stamped = _scalar(connection, f"""
                    select count(*) from media_file
                     where {live}
                       and json_extract(mf.tags, '{UUID_TAG}') is not null""")

    sections.append(_disk_section(audit))
    stale = _stale_index_check(indexed_stamped, audit)
    if stale and sections:
        sections[0].add(stale)

    sections.append(_staging_section())
    sections.append(_system_section(started_at))

    problems = sum(
        1 for section in sections for check in section.checks
        if check.status in (WARN, FAIL)
    )
    return {
        "sections": [section.as_dict() for section in sections],
        "problems": problems,
        "navidrome_error": error,
        "generated_at": time.time(),
    }
