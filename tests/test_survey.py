"""Counting what the registry would find, before anything is written.

Every category here is a real state of Alex's library, and the point of the
survey is that the two undecidable ones get counted rather than guessed at.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mutagen.easyid3 import EasyID3

from app import survey, uuidtags

SILENCE = Path(__file__).parent / "fixtures" / "silence.mp3"


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    return root


def album(root, artist, name, tracks, uuids=None, suffix=".mp3"):
    """`tracks` files of one album, carrying `uuids` (one per track, or None)."""
    folder = root / artist / name
    folder.mkdir(parents=True, exist_ok=True)
    made = []
    for n in range(1, tracks + 1):
        path = folder / f"{n:02d} - Track {n}{suffix}"
        shutil.copy(SILENCE, path)
        tags = EasyID3(path)
        tags["albumartist"] = artist
        tags["album"] = name
        tags["title"] = f"Track {n}"
        tags["tracknumber"] = str(n)
        tags.save()
        value = uuids[n - 1] if uuids else None
        if value:
            uuidtags.write(path, f"track-{artist}-{name}-{n}", value)
        made.append(path)
    return made


A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"


# --- the transcribable majority ---------------------------------------------

def test_an_album_whose_files_agree_is_ready(library):
    album(library, "The Beatles", "Abbey Road", 3, [A, A, A])

    found = survey.collect(library)

    assert [a.name for a in found.ready] == ["The Beatles - Abbey Road"]
    assert found.split == [] and found.fused == []


def test_an_album_carrying_no_uuid_at_all_is_unstamped(library):
    """A fresh UUID costs nothing: there is no Navidrome identity to lose."""
    album(library, "The Beatles", "Revolver", 3)

    found = survey.collect(library)

    assert [a.name for a in found.unstamped] == ["The Beatles - Revolver"]


def test_an_album_only_partly_stamped_is_still_transcribable(library):
    """One agreed answer, just not written onto every file yet."""
    album(library, "The Beatles", "Abbey Road", 3, [A, A, None])

    found = survey.collect(library)

    assert [a.name for a in found.partial] == ["The Beatles - Abbey Road"]
    assert found.ready == []


# --- the two that need a person ---------------------------------------------

def test_an_album_whose_files_disagree_is_split(library):
    """The old stamper took the majority among a file's neighbours, so nine
    arriving tracks could outvote the one already filed."""
    album(library, "Mk.gee", "A Museum of Contradiction", 4, [A, B, B, B])

    found = survey.collect(library)

    assert [a.name for a in found.split] == [
        "Mk.gee - A Museum of Contradiction"]
    assert found.ready == []


def test_one_uuid_across_two_albums_is_fused(library):
    """`ensure_uuid.py` assigned per directory, so a flat dump of loose
    tracks came out as one album - 101 of them, per this project's notes."""
    album(library, "The Beatles", "Abbey Road", 2, [A, A])
    album(library, "The Beatles", "Revolver", 2, [A, A])

    found = survey.collect(library)

    assert len(found.fused) == 1
    assert sorted(a.album for a in found.fused[0]) == ["Abbey Road", "Revolver"]
    assert found.ready == []


def test_a_fused_album_is_not_also_counted_as_ready(library):
    """Every album lands in exactly one bucket, worst case winning - or the
    totals overlap and the report reads as better than it is."""
    album(library, "The Beatles", "Abbey Road", 2, [A, A])
    album(library, "The Beatles", "Revolver", 2, [A, A])
    album(library, "Radiohead", "Kid A", 2, [B, B])

    found = survey.collect(library)

    assert found.albums == 3
    assert len(found.ready) == 1
    assert sum(len(group) for group in found.fused) == 2


# --- things that are not albums ---------------------------------------------

def test_a_file_with_no_album_tag_is_loose_not_an_album(library):
    """It keys on its own track UUID, so there is nothing to reconcile."""
    path = library / "stray.mp3"
    shutil.copy(SILENCE, path)

    found = survey.collect(library)

    assert found.loose == ["stray.mp3"]
    assert found.albums == 0


def test_a_wav_is_outside_the_question(library):
    """Nowhere to put the tag, so it is not a problem to report."""
    path = library / "rip.wav"
    shutil.copy(SILENCE, path)

    found = survey.collect(library)

    assert found.untaggable == 1
    assert found.files == 0


def test_an_unreadable_file_is_reported_not_swallowed(library):
    (library / "broken.mp3").write_bytes(b"not an mp3 at all")

    found = survey.collect(library)

    assert len(found.unreadable) == 1
    assert "broken.mp3" in found.unreadable[0]


def test_a_library_that_is_not_there_surveys_to_nothing(tmp_path):
    assert survey.collect(tmp_path / "gone").albums == 0


# --- it reads, and only reads -----------------------------------------------

def test_the_survey_changes_nothing(library):
    album(library, "The Beatles", "Abbey Road", 3, [A, A, None])
    album(library, "Radiohead", "Kid A", 2)
    before = {p: (p.stat().st_mtime_ns, p.read_bytes())
              for p in library.rglob("*") if p.is_file()}

    survey.collect(library)
    survey.collect(library)

    after = {p: (p.stat().st_mtime_ns, p.read_bytes())
             for p in library.rglob("*") if p.is_file()}
    assert after == before


def test_the_survey_needs_no_database(library):
    """It answers a question about files. Opening state.db to ask it would
    be the first step towards writing to it."""
    from app import store

    assert store._conn is None
    album(library, "The Beatles", "Abbey Road", 2, [A, A])

    survey.collect(library)

    assert store._conn is None


# --- the report -------------------------------------------------------------

def test_the_report_names_both_undecidable_cases(library):
    album(library, "Mk.gee", "A Museum of Contradiction", 2, [A, B])
    album(library, "The Beatles", "Abbey Road", 2, ["x-1", "x-1"])
    album(library, "The Beatles", "Revolver", 2, ["x-1", "x-1"])

    text = survey.report(survey.collect(library))

    assert "disagree" in text
    assert "sharing a UUID" in text
    assert "Nothing was written" in text


def test_the_buckets_add_up_to_every_album(library):
    album(library, "A", "One", 2, [A, A])
    album(library, "A", "Two", 2)
    album(library, "A", "Three", 2, [B, "33333333-3333-4333-8333-333333333333"])

    found = survey.collect(library)
    counts = found.as_dict()["counts"]

    assert (counts["ready"] + counts["unstamped"] + counts["partial"]
            + counts["split"] + counts["fused"]) == found.albums
