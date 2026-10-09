"""The Quarantine page: listing, restoring and deleting (app/quarantine.py).

Real MP3s, filed by the filer and set aside by the same code the Library's
Quarantine buttons use, so a restore is checked against what a quarantine
really leaves on disk.
"""

from __future__ import annotations

import shutil

from test_filer import album_on_disk, space  # noqa: F401
from app import duplicates, quarantine, store, uuidtags


def copy_of(filed, root, **fields):
    rel = filed.path.relative_to(root).as_posix()
    values = dict(id=f"id-{filed.path.stem}", path=rel, title=filed.path.stem,
                  album="Believe", artist="Aiden Williams", suffix="mp3",
                  bit_rate=320, duration=200.0, size=1, mbid="",
                  track_artist="Aiden Williams", starred=False, rating=0,
                  library_id=1, library="Music", held_by_others="")
    values.update(fields)
    return duplicates.Copy(**values)


def set_aside(space, identity, filed):  # noqa: F811
    return duplicates.quarantine_one(copy_of(filed, space.library_path), identity)


def keys(listing):
    return [t["key"] for album in listing["albums"] for t in album["tracks"]]


# --- listing ----------------------------------------------------------------------

def test_a_track_removed_by_hand_is_listed_under_its_album(space, identity):
    filed = album_on_disk(space, "Aiden Williams", "Believe", ["One", "Two"])
    set_aside(space, identity, filed[1])

    listing = quarantine.listing(identity)

    assert listing["tracks"] == 1 and listing["by_reason"] == {"removed": 1}
    album = listing["albums"][0]
    assert album["folder"] == "Aiden Williams/Believe"
    track = album["tracks"][0]
    assert track["reason"] == "removed" and track["decided_by"] == "alex"
    assert track["was"] == "Aiden Williams/Believe/02 - Two.mp3"


def test_a_file_with_no_record_is_listed_from_its_tags(space, identity):
    """Set aside by an older version, buried under the doubled folder the
    broken .ndignore marker produced - which, renamed, sits inside
    quarantine/ still carrying the old name."""
    filed = album_on_disk(space, "Mk.gee", "A Museum of Contradiction", ["goodbye"])
    buried = (space.library_path / "quarantine" / "duplicates-removed"
              / "Mk.gee" / "A Museum of Contradiction" / filed[0].path.name)
    buried.parent.mkdir(parents=True)
    shutil.move(str(filed[0].path), str(buried))

    track = quarantine.listing(identity)["albums"][0]["tracks"][0]

    assert track["reason"] == "unknown"
    assert track["title"] == "goodbye" and track["artist"] == "Mk.gee"
    assert track["was"] == "Mk.gee/A Museum of Contradiction/01 - goodbye.mp3"


def test_only_this_persons_libraries_are_listed(space, identity, tmp_path):
    hers = tmp_path / "kelly" / "quarantine" / "x" / "song.mp3"
    hers.parent.mkdir(parents=True)
    hers.write_bytes(b"audio")
    duplicates._quarantine_root(space.library_path)   # marker and README only

    assert quarantine.listing(identity) == {"albums": [], "tracks": 0, "bytes": 0,
                                            "by_reason": {}}


# --- restoring --------------------------------------------------------------------

def test_restore_puts_a_track_back_where_it_was_with_its_identity(space, identity):
    filed = album_on_disk(space, "Aiden Williams", "Believe", ["One"])
    original = filed[0].path
    identity_tags = uuidtags.read(original)
    (original.parent / "cover.jpg").write_bytes(b"jpg")
    set_aside(space, identity, filed[0])
    assert not original.exists()

    outcome = quarantine.restore(identity, keys(quarantine.listing(identity)))

    assert outcome["failed"] == []
    assert outcome["restored"][0]["to"] == "Aiden Williams/Believe/01 - One.mp3"
    assert uuidtags.read(original) == identity_tags
    # The cover went aside with the album and comes back with it.
    assert (original.parent / "cover.jpg").read_bytes() == b"jpg"
    assert quarantine.listing(identity)["tracks"] == 0
    assert store.quarantined() == []
    assert store.quarantined(include_restored=True)[0]["restored_at"]
    assert not (space.library_path / "quarantine" / "Aiden Williams").exists()


def test_restore_when_the_place_is_taken_files_it_by_its_tags(space, identity):
    filed = album_on_disk(space, "Aiden Williams", "Believe", ["One"])
    original = filed[0].path
    set_aside(space, identity, filed[0])
    # The copy kept in its place, under the same name.
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b"keeper")

    outcome = quarantine.restore(identity, keys(quarantine.listing(identity)))

    assert outcome["failed"] == []
    assert outcome["restored"][0]["to"] != "Aiden Williams/Believe/01 - One.mp3"
    assert original.read_bytes() == b"keeper", "the copy in its place is untouched"


def test_a_key_outside_the_quarantine_is_refused(space, identity):
    filed = album_on_disk(space, "Aiden Williams", "Believe", ["One"])
    escape = f"1:../{filed[0].path.relative_to(space.library_path).as_posix()}"

    outcome = quarantine.restore(identity, [escape, "2:anything.mp3", "nonsense"])

    assert outcome["restored"] == [] and len(outcome["failed"]) == 3
    assert filed[0].path.exists()


# --- deleting ---------------------------------------------------------------------

def test_delete_removes_the_file_and_keeps_the_record(space, identity):
    filed = album_on_disk(space, "Aiden Williams", "Believe", ["One"])
    (filed[0].path.parent / "cover.jpg").write_bytes(b"jpg")
    set_aside(space, identity, filed[0])

    outcome = quarantine.delete(identity, keys(quarantine.listing(identity)))

    assert len(outcome["deleted"]) == 1 and outcome["failed"] == []
    assert quarantine.listing(identity)["tracks"] == 0
    row = store.quarantined(include_restored=True)[0]
    assert row["deleted_at"] and not row["restored_at"]
    # Its folder, cover and all, is gone from the quarantine.
    assert not (space.library_path / "quarantine" / "Aiden Williams").exists()


def test_empty_deletes_only_what_is_older_than_asked(space, identity):
    filed = album_on_disk(space, "Aiden Williams", "Believe", ["Old", "New"])
    for one in filed:
        set_aside(space, identity, one)
    store.connection().execute(
        "UPDATE duplicate_quarantined SET moved_at = '2025-01-01T00:00:00+00:00'"
        " WHERE title = '01 - Old'")
    store.connection().commit()

    outcome = quarantine.empty(identity, 30)

    assert len(outcome["deleted"]) == 1
    left = quarantine.listing(identity)["albums"][0]["tracks"]
    assert [t["was"] for t in left] == ["Aiden Williams/Believe/02 - New.mp3"]


# --- duplicates-removed/ becomes quarantine/ -----------------------------------------

def _old_quarantine(space, identity, title="One"):
    """A track set aside before the rename, recorded where it then was."""
    filed = album_on_disk(space, "Aiden Williams", "Believe", [title])[0]
    rel = filed.path.relative_to(space.library_path)
    old = space.library_path / "duplicates-removed" / rel
    old.parent.mkdir(parents=True, exist_ok=True)
    (space.library_path / "duplicates-removed" / ".ndignore").write_text("")
    shutil.move(str(filed.path), str(old))
    store.record_quarantine("manual:x", copy_of(filed, space.library_path), None,
                            str(filed.path), str(old), "alex")
    return filed.path, old


def test_the_old_folder_is_renamed_and_the_record_follows(space, identity):
    original, old = _old_quarantine(space, identity)

    quarantine.rename_old_folders([space.library_path])

    new = space.library_path / "quarantine"
    assert not (space.library_path / "duplicates-removed").exists()
    assert (new / old.relative_to(space.library_path / "duplicates-removed")).is_file()
    assert (new / ".ndignore").read_bytes() == b""
    assert "Quarantine page" in (new / "README.txt").read_text(encoding="utf-8")
    assert store.quarantined()[0]["target_path"].startswith(str(new))
    # And it is still the track it was: listed with its record, restorable.
    track = quarantine.listing(identity)["albums"][0]["tracks"][0]
    assert track["reason"] == "removed"
    quarantine.restore(identity, [track["key"]])
    assert original.is_file()


def test_both_folders_are_merged_when_the_new_one_already_exists(space, identity):
    _original, old = _old_quarantine(space, identity)
    (space.library_path / "quarantine" / "Other").mkdir(parents=True)

    quarantine.rename_old_folders([space.library_path])

    assert not (space.library_path / "duplicates-removed").exists()
    assert (space.library_path / "quarantine" / "Other").is_dir()
    assert quarantine.listing(identity)["tracks"] == 1


def test_renaming_twice_does_nothing_the_second_time(space, identity):
    _old_quarantine(space, identity)
    quarantine.rename_old_folders([space.library_path])

    assert quarantine.rename_old_folders([space.library_path]) == 0
