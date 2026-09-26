"""Giving fused and split albums their own identity back.

The operation writes one tag per file and nothing else - no path changes,
no track UUID touched. What these check is mostly that: that it fixes the
identity, refuses when it cannot do so safely, and leaves everything it
was not asked about exactly as it found it.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from mutagen.easyid3 import EasyID3

from app import registry, survey, unfuse, uuidtags

SILENCE = Path(__file__).parent / "fixtures" / "silence.mp3"

A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    return root


def album(root, artist, name, tracks, uuids=None, track_uuids=True):
    folder = root / artist / name
    folder.mkdir(parents=True, exist_ok=True)
    made = []
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
        uuidtags.write(path, f"track-{name}-{n}" if track_uuids else None,
                       value)
        made.append(path)
    return made


def loose(root, stem, track_uuid, album_uuid):
    """A file with no album tag: its own record, keyed on its own UUID."""
    path = root / f"{stem}.mp3"
    shutil.copy(SILENCE, path)
    tags = EasyID3(path)
    tags["title"] = stem
    tags["artist"] = "Somebody"
    tags.save()
    uuidtags.write(path, track_uuid, album_uuid)
    return path


def planned(root, library_id=1):
    return unfuse.check(unfuse.build(survey.collect(root), library_id))


def album_uuid(path):
    return uuidtags.read(path)[1]


def track_uuid(path):
    return uuidtags.read(path)[0]


# --- fused: several albums wearing one identity ---------------------------

def test_two_albums_sharing_a_uuid_stop_sharing_it(state_db, library):
    one = album(library, "Kosu.", "Daft.", 1, [A])
    two = album(library, "Kosu.", "earthsea.", 1, [A])

    plan = planned(library)
    assert plan.blocked == []
    unfuse.apply_plan(plan, dry_run=False)

    assert album_uuid(one[0]) != album_uuid(two[0])


def test_one_album_of_a_fused_group_keeps_what_it_had(state_db, library):
    """Only the albums wrongly absorbed get new identities. Re-stamping all
    of them would throw away a record that was not wrong."""
    one = album(library, "Kosu.", "Daft.", 3, [A, A, A])
    two = album(library, "Kosu.", "earthsea.", 1, [A])

    unfuse.apply_plan(planned(library), dry_run=False)

    assert album_uuid(one[0]) == A, "the bigger album kept it"
    assert album_uuid(two[0]) != A


def test_every_file_of_a_freed_album_gets_the_same_new_uuid(state_db,
                                                             library):
    # The four-file album keeps the shared UUID, so the one-file album is
    # the one that has to be given a new identity across all of its files.
    album(library, "Kosu.", "Daft.", 4, [A, A, A, A])
    freed = album(library, "Kosu.", "earthsea.", 3, [A, A, A])

    unfuse.apply_plan(planned(library), dry_run=False)

    values = {album_uuid(p) for p in freed}
    assert len(values) == 1, "an album is one record, not four"
    assert A not in values


def test_a_fused_group_of_five_becomes_five_records(state_db, library):
    made = [album(library, "Kosu.", name, 1, [A])
            for name in ("Daft.", "earthsea.", "Eternal.", "monochrome", "thirds.")]

    unfuse.apply_plan(planned(library), dry_run=False)

    assert len({album_uuid(files[0]) for files in made}) == 5


# --- split: one album wearing two ------------------------------------------

def test_an_album_whose_files_disagree_is_brought_together(state_db, library):
    files = album(library, "Michael Jackson", "XSCAPE", 3, [A, A, B])

    unfuse.apply_plan(planned(library), dry_run=False)

    assert len({album_uuid(p) for p in files}) == 1


def test_the_majority_uuid_wins_a_split(state_db, library):
    files = album(library, "Michael Jackson", "XSCAPE", 3, [A, A, B])

    unfuse.apply_plan(planned(library), dry_run=False)

    assert album_uuid(files[0]) == A


def test_a_tied_split_is_decided_the_same_way_every_time(state_db, library):
    """No majority. The answer has to be stable, or two runs over one
    library disagree and the album splits again."""
    files = album(library, "Portraits Of Tracy", "Drive Home", 2, [B, A])

    unfuse.apply_plan(planned(library), dry_run=False)

    assert {album_uuid(p) for p in files} == {min(A, B)}


# --- loose files ------------------------------------------------------------

def test_two_untagged_files_do_not_share_an_album(state_db, library):
    """A file with no album tag is a record of one. Two of them sharing an
    identity fuses two unrelated singles into one album."""
    one = loose(library, "first", "t-1", A)
    two = loose(library, "second", "t-2", A)

    unfuse.apply_plan(planned(library), dry_run=False)

    assert album_uuid(one) != album_uuid(two)


def test_a_lone_untagged_file_is_left_alone(state_db, library):
    only = loose(library, "first", "t-1", A)

    unfuse.apply_plan(planned(library), dry_run=False)

    assert album_uuid(only) == A


# --- what it must not do ----------------------------------------------------

def test_track_uuids_are_never_touched(state_db, library):
    """The whole operation is safe because PID.Track resolves the track UUID
    first. Changing one would lose the play history this protects."""
    one = album(library, "Kosu.", "Daft.", 2, [A, A])
    two = album(library, "Kosu.", "earthsea.", 2, [A, A])
    before = {p: track_uuid(p) for p in one + two}

    unfuse.apply_plan(planned(library), dry_run=False)

    assert {p: track_uuid(p) for p in one + two} == before


def test_nothing_moves(state_db, library):
    one = album(library, "Kosu.", "Daft.", 2, [A, A])
    two = album(library, "Kosu.", "earthsea.", 2, [A, A])
    before = sorted(p.relative_to(library) for p in library.rglob("*")
                    if p.is_file())

    unfuse.apply_plan(planned(library), dry_run=False)

    after = sorted(p.relative_to(library) for p in library.rglob("*")
                   if p.is_file())
    assert after == before
    assert all(p.exists() for p in one + two)


def test_albums_that_were_already_right_are_not_rewritten(state_db, library):
    fine = album(library, "Radiohead", "Kid A", 3, [B, B, B])
    album(library, "Kosu.", "Daft.", 1, [A])
    album(library, "Kosu.", "earthsea.", 1, [A])
    before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in fine}

    unfuse.apply_plan(planned(library), dry_run=False)

    assert {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in fine} == before


def test_a_file_with_no_track_uuid_blocks_the_whole_run(state_db, library):
    """Without one, PID.Track falls through to a chain containing albumid -
    so changing the album would change the track's identity too, and its
    plays would go with it. That is the one thing this must never do."""
    album(library, "Kosu.", "Daft.", 1, [A])
    album(library, "Kosu.", "earthsea.", 1, [A], track_uuids=False)

    plan = planned(library)

    assert any("no track UUID" in reason for reason in plan.blocked)
    with pytest.raises(ValueError, match="blocked"):
        unfuse.apply_plan(plan, dry_run=False)


def test_a_blocked_plan_writes_nothing(state_db, library):
    one = album(library, "Kosu.", "Daft.", 1, [A])
    two = album(library, "Kosu.", "earthsea.", 1, [A], track_uuids=False)

    plan = planned(library)
    with pytest.raises(ValueError):
        unfuse.apply_plan(plan, dry_run=False)

    assert album_uuid(one[0]) == A and album_uuid(two[0]) == A


def test_a_dry_run_writes_nothing(state_db, library):
    one = album(library, "Kosu.", "Daft.", 1, [A])
    two = album(library, "Kosu.", "earthsea.", 1, [A])

    outcome = unfuse.apply_plan(planned(library), dry_run=True)

    # One file: the keeper already carries the right value, so only the
    # album being freed is rewritten.
    assert outcome.written == 1 and outcome.applied is False
    assert album_uuid(one[0]) == A and album_uuid(two[0]) == A


# --- the registry -----------------------------------------------------------

def test_the_registry_learns_what_the_survey_would_not_guess(state_db,
                                                              library):
    """These are exactly the albums backfill refuses to record. Once the
    ambiguity is resolved the row can finally be written."""
    album(library, "Kosu.", "Daft.", 1, [A])
    album(library, "Kosu.", "earthsea.", 1, [A])
    assert registry.known(1, registry.album_key("Kosu.", "earthsea.")) is None

    unfuse.apply_plan(planned(library), dry_run=False)

    freed = registry.known(1, registry.album_key("Kosu.", "earthsea."))
    assert freed is not None and freed != A


def test_the_registry_matches_what_is_on_disk_afterwards(state_db, library):
    album(library, "Kosu.", "Daft.", 1, [A])
    files = album(library, "Kosu.", "earthsea.", 2, [A, A])

    unfuse.apply_plan(planned(library), dry_run=False)

    assert registry.known(
        1, registry.album_key("Kosu.", "earthsea.")) == album_uuid(files[0])


def test_a_stale_registry_row_is_corrected(state_db, library):
    """reassign exists for this: uuid_for_key cannot overwrite, and here the
    recorded value is the thing that is wrong."""
    album(library, "Kosu.", "Daft.", 1, [A])
    album(library, "Kosu.", "earthsea.", 1, [A])
    registry.uuid_for_key(1, registry.album_key("Kosu.", "earthsea."),
                          on_miss=A)

    unfuse.apply_plan(planned(library), dry_run=False)

    assert registry.known(1, registry.album_key("Kosu.", "earthsea.")) != A


# --- the survey agrees it is fixed -----------------------------------------

def test_the_library_surveys_clean_afterwards(state_db, library):
    album(library, "Kosu.", "Daft.", 1, [A])
    album(library, "Kosu.", "earthsea.", 1, [A])
    album(library, "Michael Jackson", "XSCAPE", 3, [A, B, B])

    unfuse.apply_plan(planned(library), dry_run=False)

    after = survey.collect(library)
    assert after.fused == []
    assert after.split == []


def test_running_it_twice_changes_nothing_the_second_time(state_db, library):
    album(library, "Kosu.", "Daft.", 1, [A])
    two = album(library, "Kosu.", "earthsea.", 1, [A])

    unfuse.apply_plan(planned(library), dry_run=False)
    settled = album_uuid(two[0])

    second = planned(library)
    assert second.changes == []
    unfuse.apply_plan(second, dry_run=False)
    assert album_uuid(two[0]) == settled


# --- the plan is the undo ---------------------------------------------------

def test_the_saved_plan_records_what_each_file_carried(state_db, library,
                                                        tmp_path):
    album(library, "Kosu.", "Daft.", 1, [A])
    album(library, "Kosu.", "earthsea.", 1, [A])

    plan = planned(library)
    written = unfuse.save(plan, tmp_path / "plan.json")
    saved = json.loads(written.read_text(encoding="utf-8"))

    assert saved["changes"], "a plan with nothing in it undoes nothing"
    for change in saved["changes"]:
        assert change["was"] == A
        assert change["becomes"] != A
        assert (library / change["path"]).exists()


def test_the_plan_can_be_applied_backwards(state_db, library):
    """Reversible in the only sense that matters: every file ends up
    carrying exactly what it carried before."""
    one = album(library, "Kosu.", "Daft.", 1, [A])
    two = album(library, "Kosu.", "earthsea.", 2, [A, A])
    before = {p: album_uuid(p) for p in one + two}

    plan = planned(library)
    unfuse.apply_plan(plan, dry_run=False)
    assert {p: album_uuid(p) for p in one + two} != before

    for change in plan.changes:
        uuidtags.write(library / change.path, None, change.was)

    assert {p: album_uuid(p) for p in one + two} == before
