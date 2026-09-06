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

import logging
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import diskaudit, navidrome, uuidtags
from .config import settings

log = logging.getLogger("download_center.health")

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
    # Hidden unless the panel is asked to show everything. For the ones that
    # can recur but rarely do, and for facts that are status rather than
    # health. Demoted rather than deleted: twenty-one rows was too many to
    # read, but a row nobody reads is still better than a number nobody can
    # get at when it finally matters.
    secondary: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label, "value": self.value,
            "status": self.status, "detail": self.detail, "hint": self.hint,
            "secondary": self.secondary,
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

def _identity_section(connection: sqlite3.Connection, live: str,
                      user_id: str = "", audit=None) -> Section:
    """Whether every track still has a stable identity.

    This is the load-bearing one. A track without the UUID tag falls back to
    an identity derived from album/disc/track/title, which changes the moment
    anything retags the file - taking its stars and play count with it.

    The database and the disk were each asked this twice and answered in two
    rows apiece. They are the same question, and where they disagree the
    disk is right - Navidrome's index can be stale, the files cannot. One
    row each now, taken from the audit when there is one.
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
    source = "in Navidrome's index"
    if audit is not None:
        # The disk is the authority. A file stamped but not yet re-read by
        # Navidrome is not an unstamped file, and reporting it as one sends
        # you looking for something that is already done.
        unstamped = len(audit.missing_track_uuid)
        source = "read from the files themselves"
    section.add(Check(
        "unstamped", "Tracks with no UUID", unstamped,
        OK if unstamped == 0 else FAIL,
        f"{stamped} of {total - untaggable} taggable tracks carry one"
        + (f", {untaggable} cannot hold tags" if untaggable else "")
        + f" ({source})",
        "Run the stamper, then a full scan - stamping preserves mtime, so an "
        "incremental scan will not notice the new tags.",
    ))

    # This person's own annotations only. It used to count every user's, so
    # a non-admin was shown a number they could neither explain nor act on -
    # and the architecture notes claimed the whole panel was scoped.
    orphans = _scalar(connection, """
        select count(*) from annotation
         where item_type = 'media_file'
           and user_id = ?
           and (starred = 1 or rating > 0)
           and item_id not in (select id from media_file)""", user_id)
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
    if audit is not None:
        collisions = len(audit.duplicate_uuids)
    section.add(Check(
        "uuid_collisions", "Duplicate UUIDs", collisions,
        OK if collisions == 0 else FAIL,
        "distinct UUIDs claimed by more than one file",
        "A copied tag rather than a generated one. Both files collapse into "
        "a single track in Navidrome.",
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
            f"{percent}% stamped", secondary=True,
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
        # Status, not health. Behind the toggle.
        section.add(Check("last_scan", "Last scan", row[0] or "never", INFO,
                          secondary=True))

    return section


def _metadata_section(connection: sqlite3.Connection, live: str) -> Section:
    """Tagging quality. None of this is dangerous, it is just untidy."""
    section = Section("Metadata")
    columns = _columns(connection, "media_file")

    # "Tracks with no album" and "Tracks numbered zero" used to live here.
    # Both were pure noise: informational, never acted on, and between them
    # 1,127 non-zero numbers teaching you that a non-zero number here means
    # nothing. A panel of things-that-should-be-zero cannot afford rows that
    # never will be.

    if "rg_track_gain" in columns:
        total = _scalar(connection,
                        f"select count(*) from media_file mf where {live}")
        # `is not null` alone. Testing `!= 0` as well counted a track
        # legitimately measured at 0 dB as unmeasured, which is exactly the
        # value a already-normalised track gets.
        gained = _scalar(connection, f"""
            select count(*) from media_file mf
             where {live} and rg_track_gain is not null""")
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


def _duplicates_check(connection: sqlite3.Connection, identity) -> Check | None:
    """How many groups of copies are waiting to be looked at.

    Measured at 0.22s against a 6,500-track library, which is cheap enough to
    run here rather than approximating it in SQL - and an approximation would
    disagree with the number the Duplicates tab shows, which is worse than
    not having one.

    It also earns the attention dot on the menu button: without this the dot
    could not know about duplicates until you had opened that tab, which is
    exactly the moment you no longer need telling.
    """
    if identity is None:
        return None
    # Imported here rather than at module scope: duplicates imports ledger,
    # and health is imported by main before the ledger is connected.
    from . import duplicates

    try:
        groups = duplicates.find(connection, identity)
    except Exception as exc:
        log.warning("could not count duplicates for the health panel: %s", exc)
        return None

    confident = sum(1 for group in groups if group.confident)
    return Check(
        "duplicate_groups", "Duplicate recordings to review", len(groups),
        OK if not groups else WARN,
        f"{confident} share a MusicBrainz id and can be resolved in one go"
        if confident else "grouped by recording id, or by title and length",
        "The Duplicates tab. Nothing is deleted - the copy you drop moves to "
        "duplicates-removed/ inside its own library.",
    )


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
    # A count of files is a fact, not a health check. The UUID rows that used
    # to be here are merged into Identity, where the same question is asked
    # once instead of twice.
    section.add(Check(
        "disk_files", "Audio files", audit.files, INFO,
        f"read {age:.0f}h ago in {audit.seconds:.0f}s"
        + (f", {audit.untaggable} untaggable" if audit.untaggable else ""),
        secondary=True,
    ))
    section.add(Check(
        "disk_split_albums", "Directories with two album UUIDs",
        len(audit.split_albums),
        OK if not audit.split_albums else FAIL,
        "; ".join(audit.split_albums[:3]),
        "One directory is one album. Two UUIDs means Navidrome shows it "
        "twice, however tidy the folder is.",
        # An artefact of a migration that is finished. It can recur, but
        # rarely, so it waits behind the toggle rather than taking a row.
        secondary=True,
    ))
    section.add(Check(
        "disk_spanning_albums", "Album UUIDs spread across directories",
        len(audit.spanning_albums),
        OK if not audit.spanning_albums else FAIL,
        "; ".join(audit.spanning_albums[:2]),
        "The reverse of a split album: unrelated tracks fused into one "
        "record. Happens when files are stamped together and filed apart "
        "afterwards. `stamp.resplit` gives each directory its own again.",
        secondary=True,
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
    # Status, not health.
    section.add(Check("uptime", "Uptime", _duration(uptime), INFO,
                      secondary=True))

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
           libraries: list[dict[str, Any]] | None = None,
           identity=None) -> dict[str, Any]:
    """Everything the dashboard shows, for one person, in one pass.

    Scoped to the libraries they may see. An aggregate over somebody else's
    collection is not information they can act on, and reporting problems in
    it is how a panel of things-that-should-be-zero starts getting ignored.
    """
    sections: list[Section] = []
    error = None
    libraries = libraries or []
    user_id = getattr(identity, "user_id", "") or ""
    roots = [Path(lib["path"]) for lib in libraries]
    audits = [a for a in (diskaudit.cached(r) for r in roots) if a]
    audit = _merge_audits(audits)
    indexed_stamped: int | None = None
    duplicates_check: Check | None = None

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
                lambda c, l: _identity_section(c, l, user_id, audit),
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
            # Aliased `mf`, because `live` is written in terms of it. Without
            # the alias this raised "no such column: mf.missing" into a bare
            # suppress, so the stale-index check below silently never fired -
            # and that check is the only thing that can tell "never stamped"
            # from "stamped but not yet scanned". Logged rather than
            # swallowed for the same reason.
            try:
                indexed_stamped = _scalar(connection, f"""
                    select count(*) from media_file mf
                     where {live}
                       and json_extract(mf.tags, '{UUID_TAG}') is not null""")
            except sqlite3.Error as exc:
                log.warning("could not count stamped tracks in the index, so "
                            "the stale-index check is unavailable: %s", exc)

            duplicates_check = _duplicates_check(connection, identity)

    sections.append(_disk_section(audit))
    stale = _stale_index_check(indexed_stamped, audit)
    if stale and sections:
        sections[0].add(stale)

    # The Staging section is gone. It reported two counts and an age for
    # something the Staging tab shows in full, with names and sizes and a
    # button - a summary of a screen one tap away is not worth a row here.
    if duplicates_check is not None and sections:
        sections[1].add(duplicates_check)

    sections.append(_system_section(started_at))

    # Counted over the primary rows only. A badge that includes things the
    # panel does not show sends you looking for a number that is not there.
    problems = sum(
        1 for section in sections for check in section.checks
        if check.status in (WARN, FAIL) and not check.secondary
    )
    hidden = sum(1 for section in sections for check in section.checks
                 if check.secondary)
    return {
        "sections": [section.as_dict() for section in sections],
        "problems": problems,
        "hidden": hidden,
        "navidrome_error": error,
        "generated_at": time.time(),
    }
