"""The quarantine is not the library (CODE_REVIEW C4, H6).

`duplicates-removed/` sits inside each library root, and every tool that
walked a library with a plain rglob walked into it. Ten live tracks on UUID
A beside twelve set-aside copies on an older UUID B read as a "split" album
whose majority was B, so `unfuse --apply` retagged the live tracks to B -
changing the album's Navidrome identity and losing its stars and plays.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from test_unfuse import A, B, album, album_uuid, library, planned  # noqa: F401

from app import backfill, diskaudit, registry, survey, walk


def _quarantined(root, *args, **kwargs):
    quarantine = root / walk.QUARANTINE_NAME
    made = album(quarantine, *args, **kwargs)
    (quarantine / walk.NDIGNORE).write_text("", encoding="utf-8")
    return made


def test_the_walk_skips_the_quarantine_and_ignored_directories(library):
    live = album(library, "Artist", "Record", 2, [A, A])
    _quarantined(library, "Artist", "Record", 2, [B, B])
    hidden = library / "Elsewhere" / "Ignored"
    album(library, "Elsewhere", "Ignored", 1, [B])
    (hidden / walk.NDIGNORE).write_text("", encoding="utf-8")
    # A non-empty marker is a list of patterns, not "skip this directory".
    patterned = album(library, "Other", "Kept", 1, [A])
    (patterned[0].parent / walk.NDIGNORE).write_text("*.tmp\n", encoding="utf-8")

    found = [p for p in walk.library_files(library) if p.suffix == ".mp3"]

    assert found == sorted(live + patterned)


def test_quarantined_copies_do_not_make_a_live_album_split(state_db, library):
    live = album(library, "Artist", "Record", 10, [A] * 10)
    _quarantined(library, "Artist", "Record", 12, [B] * 12)

    found = survey.collect(library)
    assert found.files == 10
    assert [a.key for a in found.split] == []
    assert planned(library).changes == []
    assert {album_uuid(path) for path in live} == {A}


def test_backfill_never_records_a_quarantined_copys_uuid(state_db, library):
    _quarantined(library, "Artist", "Gone", 3, [B] * 3)

    result = backfill.run(1, library, apply=True)

    assert result.written == []
    assert registry.count(1) == 0


def test_the_disk_audit_does_not_count_quarantined_files(library):
    album(library, "Artist", "Record", 2, [A, A])
    _quarantined(library, "Artist", "Record", 3, [B] * 3)

    audit = diskaudit.run(library)

    assert audit.files == 2
    assert audit.stamped == 2
    assert audit.spanning_albums == []


def test_the_fingerprint_tool_skips_the_quarantine_too(library):
    path = Path(__file__).resolve().parent.parent / "tools" / "fingerprint.py"
    spec = importlib.util.spec_from_file_location("fingerprint_tool", path)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    live = album(library, "Artist", "Record", 1, [A])
    _quarantined(library, "Artist", "Record", 1, [B])

    assert tool.library_files(library) == live
