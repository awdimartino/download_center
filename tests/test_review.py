"""The list of music that is in the library but never confirmed.

The staging list it replaces was the music that had *not* arrived, and every
row on it was a failure. This one is the opposite: everything on it is filed
and playable, and the row only says nobody has checked it against
MusicBrainz yet.
"""

from __future__ import annotations

import pytest

from app import navidrome, review
from conftest import add_track


@pytest.fixture
def db(navidrome_db, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "navidrome_db", navidrome_db)
    return navidrome_db


@pytest.fixture
def kelly(tmp_path):
    return navidrome.Identity(
        user_id="u-kelly", username="kelly", is_admin=False, token="t",
        subsonic_token="st", subsonic_salt="ss",
        libraries=[{"id": 2, "name": "Kelly", "path": str(tmp_path / "kelly")}],
    )


def album(db, folder, count, tagged=0, library_id=1, **fields):
    """`count` tracks in one folder, `tagged` of them with a MusicBrainz id."""
    for n in range(1, count + 1):
        add_track(db, f"{folder}-{n}".replace("/", "-"),
                  path=f"{folder}/{n:02d} - Track {n}.mp3",
                  track_number=n, library_id=library_id,
                  mbz_recording_id=f"mb-{folder}-{n}" if n <= tagged else "",
                  **fields)


# --- what is listed ---------------------------------------------------------

def test_an_album_with_no_musicbrainz_ids_is_listed(db, identity):
    album(db, "The Beatles/Abbey Road", 3, album="Abbey Road",
          album_artist="The Beatles")

    listed = review.listing(identity)

    assert [e["album"] for e in listed["entries"]] == ["Abbey Road"]
    assert listed["entries"][0]["untagged"] == 3
    assert listed["entries"][0]["tracks"] == 3


def test_a_fully_confirmed_album_is_not_listed(db, identity):
    album(db, "The Beatles/Revolver", 3, tagged=3, album="Revolver")

    assert review.listing(identity)["entries"] == []


def test_an_album_with_one_unconfirmed_track_is_listed_as_partial(db,
                                                                  identity):
    """A download that joined an album already matched. "3 of 12" and "12 of
    12" call for different answers."""
    album(db, "The Beatles/Abbey Road", 12, tagged=11, album="Abbey Road")

    entry = review.listing(identity)["entries"][0]

    assert entry["untagged"] == 1
    assert entry["tracks"] == 12
    assert entry["partial"] is True


def test_untagged_is_derived_not_stored(db, identity):
    """Nothing is written down, so a track leaves the list by gaining an ID
    and no flag ever has to be cleared - which is exactly how the refusal
    table fell out of step with reality."""
    album(db, "The Beatles/Abbey Road", 1, album="Abbey Road")
    assert review.listing(identity)["entries"]

    import sqlite3
    connection = sqlite3.connect(db)
    with connection:
        connection.execute(
            "update media_file set mbz_recording_id = 'mb-1'")
    connection.close()

    assert review.listing(identity)["entries"] == []


def test_a_missing_file_is_not_listed(db, identity):
    """Navidrome still holds the row; the file has gone. Asking somebody to
    confirm a track that is not there is not work, it is noise."""
    album(db, "The Beatles/Abbey Road", 2, album="Abbey Road")
    add_track(db, "gone", path="The Beatles/Abbey Road/03 - x.mp3",
              album="Abbey Road", mbz_recording_id="", missing=1)

    assert review.listing(identity)["entries"][0]["tracks"] == 2


def test_a_file_in_a_vanished_folder_is_not_listed(db, identity):
    """`media_file.missing` alone is not enough: when a whole directory goes,
    Navidrome marks the folder rather than every file under it."""
    add_track(db, "orphan", path="Old/Album/01 - x.mp3", album="Album",
              mbz_recording_id="", folder_id="gone")

    assert review.listing(identity)["entries"] == []


def test_one_folder_is_one_album(db, identity):
    """Paths are frozen and the filer puts exactly one album in one
    directory, so the directory is the album."""
    album(db, "The Beatles/Abbey Road", 2, album="Abbey Road")
    album(db, "The Beatles/Revolver", 2, album="Revolver")

    assert len(review.listing(identity)["entries"]) == 2


# --- whose list it is -------------------------------------------------------

def test_the_list_is_private_to_the_account(db, identity, kelly):
    """An admin does not see another person's list. Kelly's experience is
    "music appears and plays"; she has a list of her own if she wants it."""
    album(db, "The Beatles/Abbey Road", 2, album="Abbey Road", library_id=1)
    album(db, "Charli xcx/BRAT", 2, album="BRAT", library_id=2)

    assert [e["album"] for e in review.listing(identity)["entries"]] \
        == ["Abbey Road"]
    assert [e["album"] for e in review.listing(kelly)["entries"]] == ["BRAT"]


def test_someone_with_no_library_sees_an_empty_list(db):
    nobody = navidrome.Identity(user_id="u-x", username="x", is_admin=True,
                                token="t", subsonic_token="", subsonic_salt="")
    assert review.listing(nobody)["entries"] == []


# --- ordering and paging ----------------------------------------------------

def test_the_newest_album_is_first(db, identity):
    album(db, "A/Old", 1, album="Old", created_at="2026-01-01T00:00:00Z")
    album(db, "A/New", 1, album="New", created_at="2026-09-01T00:00:00Z")

    assert [e["album"] for e in review.listing(identity)["entries"]] \
        == ["New", "Old"]


def test_an_album_is_dated_by_its_newest_file(db, identity):
    """A record you are still downloading belongs at the top, not halfway
    down where its first track put it."""
    add_track(db, "a", path="A/Album/01 - a.mp3", album="Album",
              mbz_recording_id="", created_at="2026-01-01T00:00:00Z")
    add_track(db, "b", path="A/Album/02 - b.mp3", album="Album",
              mbz_recording_id="", created_at="2026-09-01T00:00:00Z")
    album(db, "B/Other", 1, album="Other", created_at="2026-05-01T00:00:00Z")

    assert [e["album"] for e in review.listing(identity)["entries"]] \
        == ["Album", "Other"]


def test_the_list_is_paged(db, identity):
    for n in range(5):
        album(db, f"A/Album {n}", 1, album=f"Album {n}",
              created_at=f"2026-0{n + 1}-01T00:00:00Z")

    page = review.listing(identity, limit=2, offset=0)
    assert len(page["entries"]) == 2
    assert page["total"] == 5

    second = review.listing(identity, limit=2, offset=2)
    assert [e["album"] for e in second["entries"]] == ["Album 2", "Album 1"]


def test_a_page_size_cannot_be_unbounded(db, identity):
    """It reaches straight into a query, on a Raspberry Pi, from a button."""
    assert review.listing(identity, limit=10_000)["limit"] == review.MAX_PAGE
    assert review.listing(identity, limit=0)["limit"] == 1


def test_the_totals_say_how_far_through_this_is(db, identity):
    album(db, "A/Done", 3, tagged=3, album="Done")
    album(db, "A/Todo", 2, album="Todo")

    listed = review.listing(identity)

    assert listed["tracks"] == 5
    assert listed["untagged"] == 2


# --- opening a row ----------------------------------------------------------

def test_an_album_can_be_opened_with_its_tracks(db, identity):
    album(db, "The Beatles/Abbey Road", 3, tagged=1, album="Abbey Road")

    opened = review.entry(identity, 1, "The Beatles/Abbey Road")

    assert [t["track_no"] for t in opened["items"]] == [1, 2, 3]
    assert [t["tagged"] for t in opened["items"]] == [True, False, False]


def test_another_persons_album_cannot_be_opened(db, identity):
    album(db, "Charli xcx/BRAT", 1, album="BRAT", library_id=2)

    with pytest.raises(ValueError):
        review.entry(identity, 2, "Charli xcx/BRAT")


# --- resolving a folder to a path -------------------------------------------

def test_a_folder_resolves_inside_the_library(tmp_path, identity):
    folder = tmp_path / "music" / "The Beatles" / "Abbey Road"
    folder.mkdir(parents=True)

    assert review.album_dir(identity, 1, "The Beatles/Abbey Road") == folder


def test_a_folder_outside_the_library_is_refused(tmp_path, identity):
    """The folder arrives from the browser, so this is the boundary that has
    to hold."""
    with pytest.raises(ValueError):
        review.album_dir(identity, 1, "../../etc")


def test_a_folder_in_a_library_this_account_lacks_is_refused(tmp_path,
                                                             identity):
    (tmp_path / "kelly" / "Charli xcx").mkdir(parents=True)
    with pytest.raises(ValueError):
        review.album_dir(identity, 2, "Charli xcx")


def test_a_folder_that_is_not_on_disk_is_refused(identity):
    with pytest.raises(ValueError):
        review.album_dir(identity, 1, "The Beatles/Never Existed")


# --- when Navidrome cannot be read ------------------------------------------

def test_an_unreadable_database_is_reported_not_raised(identity, monkeypatch,
                                                       tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "navidrome_db", tmp_path / "nothing.db")

    listed = review.listing(identity)

    assert listed["available"] is False
    assert listed["entries"] == []
    assert listed["reason"]
