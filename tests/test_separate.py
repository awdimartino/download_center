"""Folders holding more than one album, separated (app/separate.py).

Real MP3s in a real library folder, filed by the filer itself and then
pushed into the wrong folder, which is the state 39 folders on the Pi are in.
"""

from __future__ import annotations

import shutil

from test_filer import album_on_disk, space, track  # noqa: F401
from app import filer, registry, separate, uuidtags


def misfile(filed, into):
    """Move filed tracks into another album's folder, as unfuse left them."""
    moved = []
    for one in filed:
        target = into / one.path.name
        one.path.rename(target)
        moved.append(target)
    left = filed[0].path.parent
    if not any(p.suffix == ".mp3" for p in left.iterdir()):
        shutil.rmtree(left)
    return moved


def test_the_album_the_folder_is_named_for_stays_and_the_other_goes_home(space):
    believe = album_on_disk(space, "Aiden Williams", "Believe", ["One"])
    breakup = album_on_disk(space, "Aiden Williams", "Breakup", ["Two", "Three"])
    folder = believe[0].path.parent
    strays = misfile(breakup, folder)
    identities = {uuidtags.read(p) for p in strays}

    plan = separate.build(space)

    # Breakup has more tracks here, but the folder is Believe's.
    assert [f.keeps for f in plan.folders] == ["Aiden Williams - Believe"]
    assert {m.album for m in plan.folders[0].moves} == {"Aiden Williams - Breakup"}

    outcome = separate.apply_plan(space, plan)

    home = space.library_path / "Aiden Williams" / "Breakup"
    assert outcome.moved == 2 and not outcome.failed
    assert sorted(p.name for p in home.iterdir()) == ["01 - Two.mp3", "02 - Three.mp3"]
    # Track and album identity go with the files, so plays and stars do too.
    assert {uuidtags.read(p) for p in home.iterdir()} == identities
    assert [p.name for p in folder.iterdir() if p.suffix == ".mp3"] == ["01 - One.mp3"]
    filer.require_one_album(folder)
    assert separate.build(space).folders == []


def test_a_stray_joins_its_album_where_that_album_already_is(space):
    album = album_on_disk(space, "Wolfmother", "Wolfmother", ["Woman", "Joker"])
    deluxe = album_on_disk(space, "Wolfmother", "Wolfmother 10TH Anniversary",
                           ["Dimension", "Colossal"])
    stray = misfile(album[1:], deluxe[0].path.parent)[0]
    # A copy with its own album UUID, as a split left it.
    uuidtags.write(stray, None, "a-stray-uuid")

    separate.apply_plan(space, separate.build(space))

    home = space.library_path / "Wolfmother" / "Wolfmother"
    assert sorted(p.name for p in home.iterdir()) == ["01 - Woman.mp3", "02 - Joker.mp3"]
    # One album, one identity: the one the registry holds for it.
    wanted = registry.known(space.library_id,
                            registry.album_key("Wolfmother", "Wolfmother"))
    assert {uuidtags.read(p)[1] for p in home.iterdir()} == {wanted}


def test_without_a_folder_named_for_any_of_them_every_album_goes_home(space):
    one = album_on_disk(space, "Kosu.", "thirds. (VIP)", ["A", "B"])
    other = album_on_disk(space, "Kosu.", "Daft.", ["C"])
    legacy = space.library_path / "Kosu_" / "Daft_"
    legacy.mkdir(parents=True)
    misfile(one, legacy)
    misfile(other, legacy)

    plan = separate.build(space)
    separate.apply_plan(space, plan)

    assert {m.album for m in plan.folders[0].moves} == {"Kosu. - thirds. (VIP)",
                                                        "Kosu. - Daft."}
    assert len(filer.audio_in(space.library_path / "Kosu" / "thirds. (VIP)")) == 2
    assert len(filer.audio_in(space.library_path / "Kosu" / "Daft")) == 1
    # Emptied and pruned.
    assert not legacy.exists()


def test_a_folder_of_one_album_under_an_old_name_is_left_alone(space):
    filed = album_on_disk(space, "voljum", "dayscapes", ["Morning", "Noon"])
    legacy = space.library_path / "voljum" / "2022 - dayscapes"
    legacy.mkdir()
    misfile(filed, legacy)

    assert separate.build(space).folders == []


def test_untagged_files_beside_an_album_stay_where_they_are(space, tmp_path):
    album = album_on_disk(space, "Valzugg", "Afternoon", ["Tea"])
    folder = album[0].path.parent
    shutil.copy(track(tmp_path, name="loose.mp3", title="Loose"), folder / "loose.mp3")

    assert separate.build(space).folders == []


def test_an_album_in_its_artists_unknown_album_folder_goes_home(space, tmp_path):
    """`Club2Tokyo/Unknown Album` held *Pink Summer* beside two loose files.
    The loose files belong there; the album does not."""
    loose = filer.file_track(space, track(tmp_path, name="l.mp3",
                                          artist="Club2Tokyo", title="Loose"))
    folder = loose.path.parent
    assert folder == space.library_path / "Club2Tokyo" / "Unknown Album"
    pink = album_on_disk(space, "Club2Tokyo", "Pink Summer", ["Sun"])
    misfile(pink, folder)

    plan = separate.build(space)
    separate.apply_plan(space, plan)

    assert plan.folders[0].keeps == "the files with no album tag"
    assert [p.name for p in filer.audio_in(folder)] == ["Loose.mp3"]
    assert len(filer.audio_in(space.library_path / "Club2Tokyo" / "Pink Summer")) == 1


def test_names_that_make_one_folder_are_reported_not_moved(space):
    album_on_disk(space, "AC/DC", "Back in Black", ["Hells Bells"])
    album_on_disk(space, "AC_DC", "Back in Black", ["Shoot to Thrill"])

    plan = separate.build(space)

    assert plan.folders == []
    assert len(plan.stuck) == 1 and "need renaming" in plan.stuck[0]


def test_the_quarantine_is_not_part_of_the_library(space):
    believe = album_on_disk(space, "Aiden Williams", "Believe", ["One"])
    other = album_on_disk(space, "Aiden Williams", "Breakup", ["Two"])
    set_aside = space.library_path / "duplicates-removed" / "Aiden Williams" / "Believe"
    set_aside.mkdir(parents=True)
    shutil.copy(believe[0].path, set_aside / "a.mp3")
    shutil.copy(other[0].path, set_aside / "b.mp3")

    assert separate.build(space).folders == []
