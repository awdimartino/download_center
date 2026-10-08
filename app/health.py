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

import copy
import logging
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import diskaudit, heartbeat, inbox, memo, navidrome, registry, uuidtags
from .config import settings

log = logging.getLogger("navidrome_companion.health")

# The tag whose value Navidrome is configured to use as its persistent track
# identity. Stored parsed in media_file.tags, so it can be queried directly
# rather than by reading several thousand files.
UUID_TAG = navidrome.UUID_TAG
ALBUM_UUID_TAG = navidrome.ALBUM_UUID_TAG

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
    # Status rather than something to act on: facts, and problems that can
    # recur but rarely do. Shown like any other row - the toggle that used to
    # hide these made you click twice to see the same panel on every visit -
    # but dimmed, and kept out of the badge. A badge that counts things you
    # cannot act on sends you looking for a problem that is not there.
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


def _scalar(connection: sqlite3.Connection, sql: str, *args) -> int:
    row = connection.execute(sql, args).fetchone()
    return (row[0] or 0) if row else 0


def _stamped(connection: sqlite3.Connection, live: str) -> int:
    """Tracks in Navidrome's index that carry the UUID tag."""
    return _scalar(connection, f"""
        select count(*) from media_file mf
         where {live} and json_extract(mf.tags, '{UUID_TAG}') is not null""")


def _attach(sections: list[Section], title: str, check: Check) -> None:
    """Add a row to the section of that title, or to a new one of that title
    if it failed to build. Found by title, not position: a section that
    failed is replaced by a "Checks unavailable" one, and a row placed by
    index landed in that."""
    for section in sections:
        if section.title == title:
            section.add(check)
            return
    sections.append(Section(title, [check]))


def _identity_section(connection: sqlite3.Connection, live: str,
                      user_id: str = "", audit=None,
                      stamped: int | None = None) -> Section:
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
    if stamped is None:
        stamped = _stamped(connection, live)

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
        "Save the album in Library: filing writes a UUID onto every track, "
        "and Navidrome reads it on its next scan.",
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
    columns = navidrome.columns_of(connection, "library")

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

    if "missing" in navidrome.columns_of(connection, "media_file"):
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
        # Status, not health.
        value, detail = _since(row[0])
        section.add(Check("last_scan", "Last scan", value, INFO, detail,
                          secondary=True))

    return section


def _metadata_section(connection: sqlite3.Connection, live: str) -> Section:
    """Tagging quality. None of this is dangerous, it is just untidy."""
    section = Section("Metadata")
    columns = navidrome.columns_of(connection, "media_file")

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
            "The Library panel measures these, one album or all at once. "
            "Navidrome also needs ReplayGain mode set to Track in personal "
            "settings before it applies them.",
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
    # Imported here rather than at module scope: duplicates imports store,
    # and health is imported by main before state.db is connected.
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
        "record. The album registry maps one album to one UUID and the "
        "filer gives it one directory, so this should now be impossible - "
        "which is exactly why it is still checked.",
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

    A tool that restores a file's mtime after tagging it - the old stamper
    did, and `tools/fingerprint.py` still does - leaves an incremental scan
    never re-reading the file, so Navidrome keeps the fallback identity. The
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
        "Whatever tagged these kept their modification time, so "
        "incremental scans skip them. Run a full scan.",
    )


# How many files to name in a row's detail before saying "and N more".
NAMED = 5


def _named(lines: list[str]) -> str:
    shown = "; ".join(lines[:NAMED])
    return shown + (f"; and {len(lines) - NAMED} more" if len(lines) > NAMED else "")


def _inbox_section(username: str | None) -> Section | None:
    """This person's inboxes as the poller last saw them.

    What the poller could not file was logged once and never mentioned
    again; only a browser upload's own finish said anything. A drop over the
    network share that could not be filed sat there unexplained.
    """
    if not username:
        return None
    entries = inbox.status_for(username)
    if not entries:
        return None
    section = Section("Inbox")
    many = len(entries) > 1
    for entry in sorted(entries, key=lambda e: e["library"]):
        label = f"Inbox: {entry['library']}" if many else "Inbox"
        key = f"inbox_{entry['library']}"
        if entry["broken"]:
            section.add(Check(key, label, "unreadable", FAIL,
                              f"Could not look in it: {entry['broken']}",
                              "The log has the traceback."))
            continue
        stuck = entry["failures"] + entry["overlooked"]
        if stuck:
            section.add(Check(
                key, label, f"{len(stuck)} not filed", WARN, _named(stuck),
                "Fix or remove these in the inbox folder; a file that "
                "changes is tried again."))
        elif entry["waiting"]:
            section.add(Check(key, label, f"{entry['waiting']} arriving", INFO,
                              "Filed once they stop changing.", secondary=True))
        else:
            section.add(Check(key, label, "empty", OK, secondary=True))
    return section


def _registry_check(libraries: list[dict[str, Any]]) -> Check | None:
    """Where the album registry and the files on disk disagree.

    Three stores say what an album is - the registry, the tags, Navidrome's
    database - updated one after another, and nothing compared them. The
    registry is what the filer obeys, so a row that has drifted from the
    files is the next save rewriting them. Read from the disk audit, so it
    costs nothing extra and is as fresh as the audit.
    """
    drifted: list[str] = []
    fused: list[str] = []
    seen = False
    for library in libraries:
        audit = diskaudit.cached(Path(library["path"]))
        if audit is None:
            continue
        seen = True
        try:
            recorded = registry.rows_for(library["id"])
        except Exception as exc:
            log.debug("could not read the registry: %s", exc)
            return None
        for folder, (key, on_disk) in sorted(audit.album_keys.items()):
            if recorded.get(key, on_disk) != on_disk:
                drifted.append(folder)
        counts: dict[str, int] = {}
        for value in recorded.values():
            counts[value] = counts.get(value, 0) + 1
        fused.extend(value for value, n in counts.items() if n > 1)
    if not seen:
        return None
    if not drifted and not fused:
        return Check("registry", "Album registry agrees with the files",
                     "yes", OK, secondary=True)
    parts = []
    if drifted:
        parts.append(f"{len(drifted)} folder(s) whose files carry a different "
                     f"album UUID from the registry: {', '.join(drifted[:5])}")
    if fused:
        parts.append(f"{len(fused)} album UUID(s) recorded under more than one name")
    return Check(
        "registry", "Album registry agrees with the files",
        f"{len(drifted) + len(fused)} disagreement(s)", WARN, "; ".join(parts),
        "Editing such an album rewrites its files to the registry's UUID "
        "unless the files agree on theirs. unfuse --apply settles fused ones.")


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
    section.add(*_loop_checks())
    section.add(_ytdlp_check())
    return section


# How old a yt-dlp release can be before it is worth a look. YouTube changes
# under it every few weeks, and the version only moves with a new image.
YTDLP_STALE_DAYS = 60


def _ytdlp_check(today: datetime | None = None) -> Check:
    """Which yt-dlp is installed, and how old it is.

    Nothing showed it. When YouTube changes, every download fails with an
    extractor error, and nothing pointed at a release that had simply aged.
    Its versions are dates, so the age is read off the version itself.
    """
    try:
        from yt_dlp.version import __version__ as version
    except Exception:
        return Check("ytdlp", "yt-dlp", "missing", FAIL,
                     "Downloads cannot run without it.")
    try:
        released = datetime.strptime(".".join(version.split(".")[:3]), "%Y.%m.%d")
    except ValueError:
        return Check("ytdlp", "yt-dlp", version, INFO, secondary=True)
    age = ((today or datetime.now()) - released).days
    if age > YTDLP_STALE_DAYS:
        return Check("ytdlp", "yt-dlp", version, WARN,
                     f"Released {age} days ago. When YouTube changes, an old "
                     "release is the usual reason every download fails.",
                     "Rebuild the image with a newer yt-dlp.")
    return Check("ytdlp", "yt-dlp", version, OK, f"Released {age} days ago",
                 secondary=True)


# The background loops, by heartbeat name: what they are called here, and
# how long without a completed pass is too long. Generous: each runs far
# more often than this, so missing it means something is wrong.
LOOPS = {
    "inbox": ("Filing the inbox", 10 * 60),
    "snapshot": ("Reading play counts", 60 * 60),
    "audit": ("Auditing the disk", 3 * 60 * 60),
}


def _loop_checks() -> list[Check]:
    """One row per background loop: when it last worked, and whether its
    last pass failed. They catch everything so they cannot die, which also
    meant a loop failing every pass for days said so only in the log."""
    beats = heartbeat.snapshot()
    checks = []
    now = time.time()
    for name, (label, too_long) in LOOPS.items():
        beat = beats.get(name)
        if beat is None:
            continue  # not run yet since the restart
        ok_at, failed_at = beat["ok_at"], beat["failed_at"]
        value = f"{_duration(now - ok_at)} ago" if ok_at else "never"
        if failed_at and (not ok_at or failed_at > ok_at):
            checks.append(Check(f"loop_{name}", label, value, FAIL,
                                f"Its last pass failed: {beat['error']}",
                                "The log has the traceback."))
        elif ok_at and now - ok_at > too_long:
            checks.append(Check(f"loop_{name}", label, value, WARN,
                                "It has not completed a pass for a while."))
        elif beat["problem"]:
            checks.append(Check(f"loop_{name}", label, value, WARN,
                                beat["problem"]))
        else:
            checks.append(Check(f"loop_{name}", label, value, OK,
                                secondary=True))
    return checks


def _since(stamp: str | None) -> tuple[str, str]:
    """A timestamp Navidrome wrote, as (how long ago, when exactly).

    Navidrome stores `2026-09-06 21:24:58.614024204+00:00` - a space for a
    separator and nanoseconds on the end - and that went into the value
    column verbatim. That column is five rem of right-aligned tabular
    numerals, sized for counts, so a 33-character timestamp arrived
    overflowing and unreadable and lined up with nothing.

    The age belongs in the column, since the question is "is the index
    stale"; the timestamp itself belongs in the detail beside it, where
    there is room for it.
    """
    if not stamp:
        return "never", "Navidrome has not scanned this library"
    try:
        # fromisoformat takes the space separator and truncates the extra
        # digits; anything it cannot read is shown raw rather than guessed at.
        scanned = datetime.fromisoformat(stamp)
    except ValueError:
        return "unknown", stamp
    age = time.time() - scanned.timestamp()
    return (f"{_duration(age)} ago",
            scanned.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC"))


def _duration(seconds: float) -> str:
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


# How long Health's Navidrome checks are kept at most. Each request ran
# about eight full scans and the duplicate finder, and every open tab asks
# every five minutes; nothing they report needs to be fresher than this.
CACHE_SECONDS = 60


def _decisions_version() -> tuple:
    """What changes the duplicates row besides the library itself."""
    from . import store

    return tuple(store.connection().execute(
        "select (select count(*) from duplicate_dismissed),"
        "       (select coalesce(max(id), 0) from duplicate_quarantined)"
    ).fetchone())


def _from_navidrome(connection: sqlite3.Connection,
                    libraries: list[dict[str, Any]], identity: Any,
                    user_id: str, audit: Any) -> tuple:
    """The checks that read Navidrome's database: (sections, the count of
    stamped tracks in the index, the duplicates row)."""
    sections: list[Section] = []
    indexed_stamped: int | None = None
    live = navidrome.live_clause(connection, [lib["id"] for lib in libraries])
    # Asked once, here, and handed to the Identity section. Aliased `mf`,
    # because `live` is written in terms of it: without the alias this
    # raised "no such column: mf.missing" into a bare suppress, so the
    # stale-index check silently never fired - and that check is the only
    # thing that can tell "never stamped" from "stamped but not yet
    # scanned". Logged rather than swallowed for the same reason; the
    # Identity section then asks again and fails on its own account.
    try:
        indexed_stamped = _stamped(connection, live)
    except sqlite3.Error as exc:
        log.warning("could not count stamped tracks in the index, so "
                    "the stale-index check is unavailable: %s", exc)
    # Navidrome's schema moves between releases, and json_extract
    # needs a SQLite built with JSON1. One section failing should
    # cost that section, not the whole panel.
    builders = (
        lambda c, l: _identity_section(c, l, user_id, audit, indexed_stamped),
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
    duplicates_check = _duplicates_check(connection, identity)
    return sections, indexed_stamped, duplicates_check


def _from_navidrome_cached(connection: sqlite3.Connection,
                           libraries: list[dict[str, Any]], identity: Any,
                           user_id: str, audit: Any) -> tuple:
    """`_from_navidrome`, kept for up to a minute while nothing it reads has
    changed: the tracks and this person's annotations, the duplicate
    decisions and quarantine record, and the disk audit. Shared, so the
    caller copies it before adding rows."""
    stamp = navidrome.library_stamp(connection, user_id or None)
    if stamp is None:
        # No cheap way to tell whether the library changed: read it fresh.
        return _from_navidrome(connection, libraries, identity, user_id, audit)
    version = (stamp, _decisions_version(),
               getattr(audit, "taken_at", None),
               int(time.time() // CACHE_SECONDS))
    key = ("health", user_id, tuple(sorted(lib["id"] for lib in libraries)))
    return memo.cached(key, version, lambda: _from_navidrome(
        connection, libraries, identity, user_id, audit))


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
            sections, indexed_stamped, duplicates_check = copy.deepcopy(
                _from_navidrome_cached(connection, libraries, identity,
                                       user_id, audit))

    sections.append(_disk_section(audit))
    stale = _stale_index_check(indexed_stamped, audit)
    if stale:
        _attach(sections, "Identity", stale)
    if duplicates_check is not None:
        _attach(sections, "Libraries", duplicates_check)
    registry_check = _registry_check(libraries)
    if registry_check is not None:
        _attach(sections, "Identity", registry_check)

    inbox_section = _inbox_section(getattr(identity, "username", None))
    if inbox_section is not None:
        sections.append(inbox_section)
    sections.append(_system_section(started_at))

    # Counted over the rows you can act on. Every row is shown now, but a
    # badge that includes status rows sends you looking for a problem that
    # is not one.
    problems = sum(
        1 for section in sections for check in section.checks
        if check.status in (WARN, FAIL) and not check.secondary
    )
    return {
        "sections": [section.as_dict() for section in sections],
        "problems": problems,
        "navidrome_error": error,
        "generated_at": time.time(),
    }
