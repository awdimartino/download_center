"""Transcribing the library's album UUIDs into the registry.

The promise this makes is narrow and worth testing as a promise: it inserts
rows and does nothing else. No tag, no filename, no track UUID, no existing
row. Several of these tests assert the absence rather than the presence,
because that is what was agreed.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mutagen.easyid3 import EasyID3

from app import backfill, registry, uuidtags

SILENCE = Path(__file__).parent / "fixtures" / "silence.mp3"

A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    return root


def album(root, artist, name, tracks, uuids=None):
    folder = root / artist / name
    folder.mkdir(parents=True, exist_ok=True)
    for n in range(1, tracks + 1):
        path = folder / f"{n:02d} - Track {n}.mp3"
        shutil.copy(SILENCE, path)
        tags = EasyID3(path)
        tags["albumartist"] = artist
        tags["album"] = name
        tags["title"] = f"Track {n}"
        tags["tracknumber"] = str(n)
        tags.save()
        value = uuids[n - 1] if uuids else None
        if value:
            uuidtags.write(path, f"track-{name}-{n}", value)
    return folder


def snapshot(root):
    return {p: (p.stat().st_mtime_ns, p.read_bytes())
            for p in root.rglob("*") if p.is_file()}


# --- what it records --------------------------------------------------------

def test_an_album_the_files_agree_on_is_recorded(state_db, library):
    album(library, "The Beatles", "Abbey Road", 3, [A, A, A])

    result = backfill.run(1, library, apply=True)

    assert [row.name for row in result.written] == ["The Beatles - Abbey Road"]
    assert registry.album_uuid_for(1, "The Beatles", "Abbey Road") == A


def test_a_partly_stamped_album_is_recorded_too(state_db, library):
    """One agreed answer, just not written onto every file yet. That does
    not stop the row being right."""
    album(library, "The Beatles", "Abbey Road", 3, [A, A, None])

    backfill.run(1, library, apply=True)

    assert registry.album_uuid_for(1, "The Beatles", "Abbey Road") == A


def test_a_later_download_joins_the_album_instead_of_splitting_it(state_db,
                                                                  library):
    """The whole point. Before the backfill, the first download of an album
    already in the library missed the table and minted a fresh UUID."""
    from app import filer, workspace

    album(library, "The Beatles", "Abbey Road", 2, [A, A])
    backfill.run(1, library, apply=True)

    space = workspace.Workspace("alex", 1, "Music", library)
    arriving = library.parent / "new.mp3"
    shutil.copy(SILENCE, arriving)
    tags = EasyID3(arriving)
    tags["albumartist"] = "The Beatles"
    tags["album"] = "Abbey Road"
    tags["title"] = "Maxwell's Silver Hammer"
    tags["tracknumber"] = "3"
    tags.save()

    assert filer.file_track(space, arriving).album_uuid == A


# --- what it refuses to touch ----------------------------------------------

def test_an_unstamped_album_is_left_alone(state_db, library):
    """A row here would give a later download a UUID no file on disk
    carries, which splits the record rather than fixing it."""
    album(library, "The Beatles", "Revolver", 3)

    result = backfill.run(1, library, apply=True)

    assert result.written == []
    assert result.skipped["unstamped"] == 1
    assert registry.count(1) == 0


def test_a_split_album_is_left_alone(state_db, library):
    album(library, "Mk.gee", "A Museum of Contradiction", 3, [A, B, B])

    result = backfill.run(1, library, apply=True)

    assert result.written == []
    assert result.skipped["split"] == 1
    assert registry.count(1) == 0


def test_fused_albums_are_left_alone(state_db, library):
    album(library, "The Beatles", "Abbey Road", 2, [A, A])
    album(library, "The Beatles", "Revolver", 2, [A, A])

    result = backfill.run(1, library, apply=True)

    assert result.written == []
    assert result.skipped["fused"] == 2
    assert registry.count(1) == 0


def test_the_clean_albums_are_still_recorded_around_the_messy_ones(state_db,
                                                                   library):
    album(library, "The Beatles", "Abbey Road", 2, [A, A])
    album(library, "Mk.gee", "A Museum of Contradiction", 2, [B, "other"])
    album(library, "Radiohead", "Kid A", 2)

    result = backfill.run(1, library, apply=True)

    assert [row.name for row in result.written] == ["The Beatles - Abbey Road"]
    assert registry.count(1) == 1


# --- it writes rows and nothing else ---------------------------------------

def test_no_file_is_touched(state_db, library):
    album(library, "The Beatles", "Abbey Road", 3, [A, A, None])
    album(library, "Radiohead", "Kid A", 2)
    album(library, "Mk.gee", "Contradiction", 2, [A, B])
    before = snapshot(library)

    backfill.run(1, library, apply=True)

    assert snapshot(library) == before


def test_a_dry_run_writes_no_row_either(state_db, library):
    album(library, "The Beatles", "Abbey Road", 3, [A, A, A])

    result = backfill.run(1, library, apply=False)

    assert [row.name for row in result.written] == ["The Beatles - Abbey Road"]
    assert registry.count(1) == 0, "a dry run must not record anything"


def test_running_it_twice_does_nothing_the_second_time(state_db, library):
    album(library, "The Beatles", "Abbey Road", 3, [A, A, A])

    backfill.run(1, library, apply=True)
    second = backfill.run(1, library, apply=True)

    assert second.written == []
    assert [row.name for row in second.already] == ["The Beatles - Abbey Road"]
    assert registry.count(1) == 1


def test_an_existing_row_is_never_overwritten(state_db, library):
    """Where the table and the disk disagree, changing either one is a
    decision about which is right."""
    album(library, "The Beatles", "Abbey Road", 2, [A, A])
    key = registry.album_key("The Beatles", "Abbey Road")
    registry.uuid_for_key(1, key, on_miss=B)

    result = backfill.run(1, library, apply=True)

    assert result.written == []
    assert len(result.conflicts) == 1
    row, recorded = result.conflicts[0]
    assert (row.album_uuid, recorded) == (A, B)
    assert registry.known(1, key) == B


def test_another_library_is_not_written_to(state_db, library):
    album(library, "The Beatles", "Abbey Road", 2, [A, A])

    backfill.run(1, library, apply=True)

    assert registry.count(1) == 1
    assert registry.count(2) == 0


def test_a_library_that_is_not_there_records_nothing(state_db, tmp_path):
    result = backfill.run(1, tmp_path / "gone", apply=True)
    assert result.written == []
    assert registry.count() == 0


# --- the report -------------------------------------------------------------

def test_the_report_says_no_file_was_written(state_db, library):
    album(library, "The Beatles", "Abbey Road", 2, [A, A])
    text = backfill.report(backfill.run(1, library, apply=True))
    assert "No file was opened for writing." in text


def test_a_dry_run_says_how_to_apply(state_db, library):
    album(library, "The Beatles", "Abbey Road", 2, [A, A])
    text = backfill.report(backfill.run(1, library, apply=False))
    assert "--apply" in text


def test_the_report_counts_what_it_left_alone(state_db, library):
    album(library, "Radiohead", "Kid A", 2)
    album(library, "Mk.gee", "Contradiction", 2, [A, B])

    text = backfill.report(backfill.run(1, library, apply=True))

    assert "carrying no album UUID" in text
    assert "disagree" in text
    assert "app.survey" in text
