"""Every album a person owns, and which of them MusicBrainz knows.

This was the Review list, which showed only albums with something
unconfirmed - and that made a matched album unreachable, because it dropped
off the only list carrying the button that could correct it. So the list is
everything and the narrowing is a filter.
"""

from __future__ import annotations

import pytest

from app import library, navidrome
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


def make_album(db, folder, count, tagged=0, library_id=1, **fields):
    """`count` tracks in one folder, `tagged` of them with a MusicBrainz id."""
    for n in range(1, count + 1):
        add_track(db, f"{folder}-{n}".replace("/", "-"),
                  path=f"{folder}/{n:02d} - Track {n}.mp3",
                  track_number=n, library_id=library_id,
                  mbz_recording_id=f"mb-{folder}-{n}" if n <= tagged else "",
                  **fields)


# --- what is listed ---------------------------------------------------------

def test_an_album_with_no_musicbrainz_ids_is_listed(db, identity):
    make_album(db, "The Beatles/Abbey Road", 3, album="Abbey Road",
          album_artist="The Beatles")

    listed = library.listing(identity)

    assert [e["album"] for e in listed["albums"]] == ["Abbey Road"]
    assert listed["albums"][0]["untagged"] == 3
    assert listed["albums"][0]["tracks"] == 3


def test_a_fully_matched_album_is_still_listed(db, identity):
    """The whole reason this stopped being the Review list. A matched album
    used to vanish, taking with it the only way to correct a wrong match."""
    make_album(db, "The Beatles/Revolver", 3, tagged=3, album="Revolver")

    listed = library.listing(identity)["albums"]

    assert [a["album"] for a in listed] == ["Revolver"]
    assert listed[0]["matched"] is True
    assert listed[0]["untagged"] == 0


def test_the_filter_narrows_to_albums_without_a_match(db, identity):
    make_album(db, "The Beatles/Revolver", 3, tagged=3, album="Revolver")
    make_album(db, "The Beatles/Abbey Road", 3, album="Abbey Road")

    every = library.listing(identity)["albums"]
    narrowed = library.listing(identity, unmatched_only=True)["albums"]

    assert len(every) == 2
    assert [a["album"] for a in narrowed] == ["Abbey Road"]


def test_an_album_with_one_unconfirmed_track_is_listed_as_partial(db,
                                                                  identity):
    """A download that joined an album already matched. "3 of 12" and "12 of
    12" call for different answers."""
    make_album(db, "The Beatles/Abbey Road", 12, tagged=11, album="Abbey Road")

    entry = library.listing(identity)["albums"][0]

    assert entry["untagged"] == 1
    assert entry["tracks"] == 12
    assert entry["partial"] is True


def test_whether_a_match_exists_is_derived_not_stored(db, identity):
    """Nothing is written down, so an album leaves the filter by gaining an
    ID and no flag ever has to be cleared - which is exactly how the refusal
    table fell out of step with reality."""
    make_album(db, "The Beatles/Abbey Road", 1, album="Abbey Road")
    assert library.listing(identity, unmatched_only=True)["albums"]

    import sqlite3
    connection = sqlite3.connect(db)
    with connection:
        connection.execute("update media_file set mbz_recording_id = 'mb-1'")
    connection.close()

    assert library.listing(identity, unmatched_only=True)["albums"] == []
    assert len(library.listing(identity)["albums"]) == 1


def test_a_missing_file_is_not_listed(db, identity):
    """Navidrome still holds the row; the file has gone. Asking somebody to
    confirm a track that is not there is not work, it is noise."""
    make_album(db, "The Beatles/Abbey Road", 2, album="Abbey Road")
    add_track(db, "gone", path="The Beatles/Abbey Road/03 - x.mp3",
              album="Abbey Road", mbz_recording_id="", missing=1)

    assert library.listing(identity)["albums"][0]["tracks"] == 2


def test_a_file_in_a_vanished_folder_is_not_listed(db, identity):
    """`media_file.missing` alone is not enough: when a whole directory goes,
    Navidrome marks the folder rather than every file under it."""
    add_track(db, "orphan", path="Old/Album/01 - x.mp3", album="Album",
              mbz_recording_id="", folder_id="gone")

    assert library.listing(identity)["albums"] == []


def test_one_folder_is_one_album(db, identity):
    """Paths are frozen and the filer puts exactly one album in one
    directory, so the directory is the album."""
    make_album(db, "The Beatles/Abbey Road", 2, album="Abbey Road")
    make_album(db, "The Beatles/Revolver", 2, album="Revolver")

    assert len(library.listing(identity)["albums"]) == 2


# --- whose list it is -------------------------------------------------------

def test_the_list_is_private_to_the_account(db, identity, kelly):
    """An admin does not see another person's list. Kelly's experience is
    "music appears and plays"; she has a list of her own if she wants it."""
    make_album(db, "The Beatles/Abbey Road", 2, album="Abbey Road", library_id=1)
    make_album(db, "Charli xcx/BRAT", 2, album="BRAT", library_id=2)

    assert [e["album"] for e in library.listing(identity)["albums"]] \
        == ["Abbey Road"]
    assert [e["album"] for e in library.listing(kelly)["albums"]] == ["BRAT"]


def test_someone_with_no_library_sees_an_empty_list(db):
    nobody = navidrome.Identity(user_id="u-x", username="x", is_admin=True,
                                token="t", subsonic_token="", subsonic_salt="")
    assert library.listing(nobody)["albums"] == []


# --- ordering and paging ----------------------------------------------------

def test_the_newest_album_is_first(db, identity):
    make_album(db, "A/Old", 1, album="Old", created_at="2026-01-01T00:00:00Z")
    make_album(db, "A/New", 1, album="New", created_at="2026-09-01T00:00:00Z")

    assert [e["album"] for e in library.listing(identity)["albums"]] \
        == ["New", "Old"]


def test_an_album_is_dated_by_its_newest_file(db, identity):
    """A record you are still downloading belongs at the top, not halfway
    down where its first track put it."""
    add_track(db, "a", path="A/Album/01 - a.mp3", album="Album",
              mbz_recording_id="", created_at="2026-01-01T00:00:00Z")
    add_track(db, "b", path="A/Album/02 - b.mp3", album="Album",
              mbz_recording_id="", created_at="2026-09-01T00:00:00Z")
    make_album(db, "B/Other", 1, album="Other", created_at="2026-05-01T00:00:00Z")

    assert [e["album"] for e in library.listing(identity)["albums"]] \
        == ["Album", "Other"]


def test_the_list_is_paged(db, identity):
    for n in range(5):
        make_album(db, f"A/Album {n}", 1, album=f"Album {n}",
              created_at=f"2026-0{n + 1}-01T00:00:00Z")

    page = library.listing(identity, limit=2, offset=0)
    assert len(page["albums"]) == 2
    assert page["total"] == 5

    second = library.listing(identity, limit=2, offset=2)
    assert [e["album"] for e in second["albums"]] == ["Album 2", "Album 1"]


def test_a_page_size_cannot_be_unbounded(db, identity):
    """It reaches straight into a query, on a Raspberry Pi, from a button."""
    assert library.listing(identity, limit=10_000)["limit"] == library.MAX_PAGE
    assert library.listing(identity, limit=0)["limit"] == 1


def test_the_totals_describe_the_library_not_the_page(db, identity):
    """They are what the filter is a filter *of*, so applying it must not
    move them."""
    make_album(db, "A/Done", 3, tagged=3, album="Done")
    make_album(db, "A/Todo", 2, album="Todo")

    for listed in (library.listing(identity),
                   library.listing(identity, unmatched_only=True),
                   library.listing(identity, search="Todo")):
        assert listed["tracks"] == 5
        assert listed["albums_total"] == 2
        assert listed["unmatched"] == 2
        assert listed["unmatched_albums"] == 1


# --- opening a row ----------------------------------------------------------


# --- resolving a folder to a path -------------------------------------------

def test_a_folder_resolves_inside_the_library(tmp_path, identity):
    folder = tmp_path / "music" / "The Beatles" / "Abbey Road"
    folder.mkdir(parents=True)

    assert library.album_dir(identity, 1, "The Beatles/Abbey Road") == folder


def test_a_folder_outside_the_library_is_refused(tmp_path, identity):
    """The folder arrives from the browser, so this is the boundary that has
    to hold."""
    with pytest.raises(ValueError):
        library.album_dir(identity, 1, "../../etc")


def test_a_folder_in_a_library_this_account_lacks_is_refused(tmp_path,
                                                             identity):
    (tmp_path / "kelly" / "Charli xcx").mkdir(parents=True)
    with pytest.raises(ValueError):
        library.album_dir(identity, 2, "Charli xcx")


def test_a_folder_that_is_not_on_disk_is_refused(identity):
    with pytest.raises(ValueError):
        library.album_dir(identity, 1, "The Beatles/Never Existed")


# --- when Navidrome cannot be read ------------------------------------------

def test_an_unreadable_database_is_reported_not_raised(identity, monkeypatch,
                                                       tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "navidrome_db", tmp_path / "nothing.db")

    listed = library.listing(identity)

    assert listed["available"] is False
    assert listed["albums"] == []
    assert listed["reason"]


# --- a file with no album folder --------------------------------------------
#
# `root / ""` is the root, so an empty folder used to resolve to the library
# itself and the traversal guard waved it through - which would have handed a
# matcher the entire library as though it were one release.

def test_a_file_at_the_library_root_has_no_album_folder(db, identity):
    add_track(db, "loose", path="loose.mp3", album="", album_artist="",
              mbz_recording_id="")
    assert library._folder_of("loose.mp3") == ""


@pytest.mark.parametrize("folder", ["", " ", "/", "\\"])
def test_the_library_root_is_never_an_album(identity, folder):
    with pytest.raises(ValueError):
        library.album_dir(identity, 1, folder)


def test_a_real_folder_still_resolves(tmp_path, identity):
    (tmp_path / "music" / "The Beatles" / "Abbey Road").mkdir(parents=True)
    assert library.album_dir(identity, 1, "The Beatles/Abbey Road").is_dir()


# --- the "in library" badge -------------------------------------------------
#
# It keys on a string built at both ends, and the two ends disagreed: the
# card carries Spotify's full credit ("Michael Jackson, Paul McCartney")
# while the file carries the primary artist alone, so every collaboration
# came up unmarked - which is exactly when a second copy gets queued.

def test_a_solo_track_in_the_library_is_marked(db, identity, monkeypatch):
    from app import main
    add_track(db, "t1", title="Billie Jean", artist="Michael Jackson")

    cards = main._mark_held([{"name": "Billie Jean",
                              "artist": "Michael Jackson",
                              "primary_artist": "Michael Jackson"}], 1)
    assert cards[0]["held"] is True


def test_a_collaboration_is_marked_too(db, identity, monkeypatch):
    from app import main
    # What tagger.tag writes: the primary artist, not the full credit.
    add_track(db, "t1", title="The Girl Is Mine", artist="Michael Jackson")

    cards = main._mark_held([{"name": "The Girl Is Mine",
                              "artist": "Michael Jackson, Paul McCartney",
                              "primary_artist": "Michael Jackson"}], 1)
    assert cards[0]["held"] is True


def test_a_file_tagged_with_the_full_credit_is_marked(db, identity):
    """A CD rip or a hand-tagged file may have it the other way round."""
    from app import main
    add_track(db, "t1", title="The Girl Is Mine",
              artist="Michael Jackson, Paul McCartney",
              album_artist="Michael Jackson")

    cards = main._mark_held([{"name": "The Girl Is Mine",
                              "artist": "Michael Jackson, Paul McCartney",
                              "primary_artist": "Michael Jackson"}], 1)
    assert cards[0]["held"] is True


def test_a_track_the_library_does_not_have_is_not_marked(db, identity):
    from app import main
    add_track(db, "t1", title="Billie Jean", artist="Michael Jackson")

    cards = main._mark_held([{"name": "Thriller",
                              "artist": "Michael Jackson",
                              "primary_artist": "Michael Jackson"}], 1)
    assert cards[0]["held"] is False


def test_the_badge_is_scoped_to_the_library(db, identity):
    from app import main
    add_track(db, "t1", title="Billie Jean", artist="Michael Jackson",
              library_id=2)

    cards = main._mark_held([{"name": "Billie Jean",
                              "artist": "Michael Jackson",
                              "primary_artist": "Michael Jackson"}], 1)
    assert cards[0]["held"] is False


# --- what counts as an album folder -----------------------------------------
#
# A retag applies to every file under the folder it is given, so being wrong
# here merges records permanently.

@pytest.mark.parametrize("folder", ["", " ", "/", "\\", "Radiohead",
                                    "Radiohead/Kid A/Disc 1", "a/b/c/d"])
def test_only_an_artist_slash_album_folder_is_an_album(identity, folder):
    with pytest.raises(ValueError):
        library.album_dir(identity, 1, folder)


def test_an_artist_directory_is_refused(tmp_path, identity):
    """It holds that artist's whole discography, and a retag would file all
    of it as one release."""
    (tmp_path / "music" / "Radiohead" / "Kid A").mkdir(parents=True)
    (tmp_path / "music" / "Radiohead" / "OK Computer").mkdir(parents=True)

    with pytest.raises(ValueError):
        library.album_dir(identity, 1, "Radiohead")


def test_the_album_folder_itself_still_resolves(tmp_path, identity):
    (tmp_path / "music" / "Radiohead" / "Kid A").mkdir(parents=True)
    assert library.album_dir(identity, 1, "Radiohead/Kid A").is_dir()


def test_a_navidrome_schema_this_release_lacks_is_reported(identity,
                                                           monkeypatch,
                                                           tmp_path):
    """Not a 500. The browser renders that as "everything has been matched"."""
    import sqlite3
    from app.config import settings

    path = tmp_path / "odd.db"
    connection = sqlite3.connect(path)
    with connection:
        connection.execute("create table media_file (id text, library_id int)")
    connection.close()
    monkeypatch.setattr(settings, "navidrome_db", path)

    listed = library.listing(identity)

    assert listed["available"] is False
    assert listed["reason"]


# --- searching --------------------------------------------------------------

def test_search_matches_the_album(db, identity):
    make_album(db, "The Beatles/Abbey Road", 1, album="Abbey Road")
    make_album(db, "The Beatles/Revolver", 1, album="Revolver")

    found = library.listing(identity, search="abbey")["albums"]

    assert [a["album"] for a in found] == ["Abbey Road"]


def test_search_matches_the_artist(db, identity):
    make_album(db, "The Beatles/Abbey Road", 1, album="Abbey Road",
               album_artist="The Beatles")
    make_album(db, "Radiohead/Kid A", 1, album="Kid A",
               album_artist="Radiohead")

    found = library.listing(identity, search="radio")["albums"]

    assert [a["album"] for a in found] == ["Kid A"]


def test_search_ignores_case_and_surrounding_space(db, identity):
    make_album(db, "The Beatles/Abbey Road", 1, album="Abbey Road")

    for typed in ("  ABBEY  ", "abbey", " Abbey"):
        assert len(library.listing(identity, search=typed)["albums"]) == 1


def test_search_and_the_filter_apply_together(db, identity):
    make_album(db, "The Beatles/Abbey Road", 1, tagged=1, album="Abbey Road")
    make_album(db, "The Beatles/Abbey Road Sessions", 1,
               album="Abbey Road Sessions")

    found = library.listing(identity, unmatched_only=True,
                            search="abbey")["albums"]

    assert [a["album"] for a in found] == ["Abbey Road Sessions"]


def test_a_search_that_matches_nothing_is_empty_not_everything(db, identity):
    make_album(db, "The Beatles/Abbey Road", 1, album="Abbey Road")
    assert library.listing(identity, search="zzzz")["albums"] == []


# --- opening an album -------------------------------------------------------

def test_an_albums_tracks_can_be_read(db, identity):
    make_album(db, "The Beatles/Abbey Road", 3, tagged=1, album="Abbey Road")

    opened = library.tracks(identity, 1, "The Beatles/Abbey Road")

    assert [t["track_no"] for t in opened["items"]] == [1, 2, 3]
    assert [t["tagged"] for t in opened["items"]] == [True, False, False]


def test_tracks_are_ordered_by_disc_then_number(db, identity):
    add_track(db, "b", path="A/B/x.mp3", track_number=1, disc_number=2,
              title="Disc two, one", mbz_recording_id="")
    add_track(db, "a", path="A/B/y.mp3", track_number=9, disc_number=1,
              title="Disc one, nine", mbz_recording_id="")

    opened = library.tracks(identity, 1, "A/B")

    assert [t["title"] for t in opened["items"]] == [
        "Disc one, nine", "Disc two, one"]


def test_only_that_albums_tracks_come_back(db, identity):
    make_album(db, "The Beatles/Abbey Road", 2, album="Abbey Road")
    make_album(db, "The Beatles/Revolver", 3, album="Revolver")

    opened = library.tracks(identity, 1, "The Beatles/Revolver")

    assert len(opened["items"]) == 3


def test_another_persons_album_cannot_be_opened(db, identity):
    make_album(db, "Charli xcx/BRAT", 1, album="BRAT", library_id=2)

    with pytest.raises(ValueError):
        library.tracks(identity, 2, "Charli xcx/BRAT")


def test_an_album_that_is_not_there_is_refused(db, identity):
    with pytest.raises(ValueError):
        library.tracks(identity, 1, "Nobody/Nothing")


# --- the boundary a track path has to clear ---------------------------------

def test_a_track_resolves_inside_the_library(tmp_path, identity):
    folder = tmp_path / "music" / "The Beatles" / "Abbey Road"
    folder.mkdir(parents=True)
    (folder / "01 - Come Together.mp3").write_bytes(b"x")

    got = library.track_path(identity, 1,
                             "The Beatles/Abbey Road/01 - Come Together.mp3")

    assert got == folder / "01 - Come Together.mp3"


@pytest.mark.parametrize("path", ["", "   ", "../../../etc/passwd", "/etc/passwd"])
def test_a_path_that_leaves_the_library_is_refused(tmp_path, identity, path):
    with pytest.raises(ValueError):
        library.track_path(identity, 1, path)


def test_a_directory_is_not_a_track(tmp_path, identity):
    """An editor handed a folder would treat it as one file."""
    (tmp_path / "music" / "The Beatles").mkdir(parents=True)
    with pytest.raises(ValueError):
        library.track_path(identity, 1, "The Beatles")


def test_a_track_in_another_persons_library_is_refused(tmp_path, identity):
    (tmp_path / "kelly" / "A").mkdir(parents=True)
    (tmp_path / "kelly" / "A" / "x.mp3").write_bytes(b"x")
    with pytest.raises(ValueError):
        library.track_path(identity, 2, "A/x.mp3")


def test_a_track_that_is_not_on_disk_is_refused(tmp_path, identity):
    (tmp_path / "music").mkdir(exist_ok=True)
    with pytest.raises(ValueError):
        library.track_path(identity, 1, "Nobody/Nothing/gone.mp3")
