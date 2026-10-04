"""Every album a person owns, and which of them MusicBrainz knows.

This was the Review list, which showed only albums with something
unconfirmed - and that made a matched album unreachable, because it dropped
off the only list carrying the button that could correct it. So the list is
everything and the narrowing is a filter.
"""

from __future__ import annotations

import json

import pytest

from app import library, navidrome
from conftest import add_track


@pytest.fixture
def db(navidrome_db, state_db, monkeypatch):
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
    fields.setdefault("album_id", f"al-{folder}")
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
    narrowed = library.listing(identity, show="unmatched")["albums"]

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
    assert library.listing(identity, show="unmatched")["albums"]

    import sqlite3
    connection = sqlite3.connect(db)
    with connection:
        connection.execute("update media_file set mbz_recording_id = 'mb-1'")
    connection.close()

    assert library.listing(identity, show="unmatched")["albums"] == []
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
                   library.listing(identity, show="unmatched"),
                   library.listing(identity, search="Todo")):
        assert listed["tracks"] == 5
        assert listed["albums_total"] == 2
        assert listed["unmatched"] == 2
        assert listed["unmatched_albums"] == 1


# --- needs review -----------------------------------------------------------
#
# "No MusicBrainz match" never empties: a hand-tagged bootleg sits in it for
# ever, correctly. "Needs review" is the part of it nobody has dealt with.

def test_an_unmatched_album_needs_review_until_someone_deals_with_it(
        db, identity):
    from app import store
    make_album(db, "A/Bootleg", 2, album="Bootleg")

    assert [a["album"] for a in
            library.listing(identity, show="review")["albums"]] == ["Bootleg"]

    store.mark_reviewed(1, library.album_ids(identity, 1, "A/Bootleg"),
                        "marked", "alex")

    assert library.listing(identity, show="review")["albums"] == []
    # Still no MusicBrainz match - that filter is about MusicBrainz.
    assert len(library.listing(identity, show="unmatched")["albums"]) == 1
    assert library.listing(identity)["review_albums"] == 0


def test_a_matched_album_never_needs_review(db, identity):
    make_album(db, "A/Done", 2, tagged=2, album="Done")
    assert library.listing(identity, show="review")["albums"] == []


def test_review_survives_a_rename(db, identity):
    """Keyed on Navidrome's album id, which follows the album UUID, not on
    the folder - a rename moves the folder and keeps the id."""
    import sqlite3
    from app import store
    make_album(db, "A/Old Name", 1, album="Old Name", album_id="al-x")
    store.mark_reviewed(1, {"al-x"}, "edited", "alex")

    connection = sqlite3.connect(db)
    with connection:
        connection.execute("update media_file set path = 'A/New Name/01.mp3'")
    connection.close()

    assert library.listing(identity)["albums"][0]["reviewed"] is True


def test_a_folder_holding_two_albums_is_reviewed_only_when_both_are(
        db, identity):
    from app import store
    add_track(db, "a", path="A/Mixed/01.mp3", album_id="al-1")
    add_track(db, "b", path="A/Mixed/02.mp3", album_id="al-2")
    store.mark_reviewed(1, {"al-1"}, "marked", "alex")

    assert library.listing(identity)["albums"][0]["needs_review"] is True


def test_review_marks_are_per_library(db, identity):
    from app import store
    make_album(db, "A/Bootleg", 1, album="Bootleg", album_id="al-same")
    store.mark_reviewed(2, {"al-same"}, "marked", "kelly")

    assert library.listing(identity)["albums"][0]["reviewed"] is False


def test_an_unknown_filter_is_refused(db, identity):
    with pytest.raises(ValueError):
        library.listing(identity, show="everything")


# --- ReplayGain -------------------------------------------------------------

def test_tracks_without_replaygain_are_counted(db, identity):
    """Null is unmeasured; 0.0 dB is a measurement (FIXES item 22)."""
    add_track(db, "a", path="A/X/01.mp3", rg_track_gain=None, album_id="x")
    add_track(db, "b", path="A/X/02.mp3", rg_track_gain=0.0, album_id="x")
    add_track(db, "c", path="A/Y/01.mp3", rg_track_gain=-6.1, album_id="y")

    listed = library.listing(identity)
    by_folder = {a["folder"]: a for a in listed["albums"]}

    assert by_folder["A/X"]["no_gain"] == 1
    assert by_folder["A/Y"]["no_gain"] == 0
    assert listed["no_gain"] == 1
    assert listed["no_gain_albums"] == 1
    assert [a["folder"] for a in
            library.listing(identity, show="nogain")["albums"]] == ["A/X"]


def test_the_albums_to_measure_are_the_ones_missing_gain(db, identity):
    add_track(db, "a", path="A/X/01.mp3", rg_track_gain=None, album_id="x")
    add_track(db, "c", path="A/Y/01.mp3", rg_track_gain=-6.1, album_id="y")
    add_track(db, "k", path="K/Z/01.mp3", rg_track_gain=None, library_id=2)

    assert library.without_gain(identity) == [(1, "A/X")]


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
    assert library.folder_of("loose.mp3") == ""


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


# --- the album covers' "3 of 11 in library" ---------------------------------

def _album_card(**extra):
    return {"name": "Thriller", "artist": "Michael Jackson",
            "primary_artist": "Michael Jackson", "total": 9, **extra}


def test_an_album_counts_the_tracks_the_library_has(db, identity):
    from app import main
    for n in range(3):
        add_track(db, f"t{n}", title=f"Song {n}", artist="Michael Jackson",
                  album_artist="Michael Jackson", album="Thriller")

    cards = main._mark_albums_held([_album_card()], 1)
    assert cards[0]["held_tracks"] == 3


def test_an_album_filed_under_the_primary_artist_is_counted(db, identity):
    """The card carries the full credit; the file may carry only the first."""
    from app import main
    add_track(db, "t1", title="The Girl Is Mine", artist="Michael Jackson",
              album_artist="Michael Jackson", album="Thriller")

    cards = main._mark_albums_held(
        [_album_card(artist="Michael Jackson, Paul McCartney")], 1)
    assert cards[0]["held_tracks"] == 1


def test_an_album_count_never_exceeds_the_album(db, identity):
    """A deluxe edition filed under the plain title must not read 12 of 9."""
    from app import main
    for n in range(12):
        add_track(db, f"t{n}", title=f"Song {n}", artist="Michael Jackson",
                  album_artist="Michael Jackson", album="Thriller")

    cards = main._mark_albums_held([_album_card()], 1)
    assert cards[0]["held_tracks"] == 9


def test_an_album_count_is_scoped_to_the_library(db, identity):
    from app import main
    add_track(db, "t1", title="Billie Jean", artist="Michael Jackson",
              album_artist="Michael Jackson", album="Thriller", library_id=2)

    cards = main._mark_albums_held([_album_card()], 1)
    assert cards[0]["held_tracks"] == 0


def test_searching_everything_is_one_request_to_spotify(monkeypatch):
    from app import spotify
    asked = []

    class Client:
        def search(self, q, type, limit):
            asked.append(type)
            return {
                "albums": {"items": [{"id": "a1", "name": "Thriller",
                                      "artists": [{"name": "Michael Jackson"}],
                                      "release_date": "1982-11-30",
                                      "total_tracks": 9, "images": []}, None]},
                "tracks": {"items": [{"id": "t1", "name": "Billie Jean",
                                      "artists": [{"name": "Michael Jackson"}],
                                      "album": {"name": "Thriller"}}]},
                "artists": {"items": [{"id": "r1", "name": "Michael Jackson"}]},
            }

    monkeypatch.setattr(spotify, "client", lambda: Client())
    found = spotify.browse_all("thriller")
    assert asked == ["album,track,artist"]
    assert [a["name"] for a in found["albums"]] == ["Thriller"]
    assert [t["name"] for t in found["tracks"]] == ["Billie Jean"]
    assert [a["name"] for a in found["artists"]] == ["Michael Jackson"]


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

    found = library.listing(identity, show="unmatched",
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


# --- genre tally --------------------------------------------------------

def genre_tag(genre: str) -> str:
    return json.dumps({"genre": [{"value": genre}]})


def test_genre_tally_counts_tracks_per_genre(db, identity):
    add_track(db, "t1", tags=genre_tag("Ambient"))
    add_track(db, "t2", path="b.mp3", tags=genre_tag("Ambient"))
    add_track(db, "t3", path="c.mp3", tags=genre_tag("Rock"))

    tally = library.genre_tally(identity)

    assert tally["genres"] == [
        {"genre": "Ambient", "tracks": 2}, {"genre": "Rock", "tracks": 1}]


def test_genre_tally_keeps_case_variants_separate(db, identity):
    """The point is to surface "Electronic" vs "electronic" for a later
    merge, not to paper over them here."""
    add_track(db, "t1", tags=genre_tag("Electronic"))
    add_track(db, "t2", path="b.mp3", tags=genre_tag("electronic"))

    genres = library.genre_tally(identity)["genres"]

    assert {g["genre"] for g in genres} == {"Electronic", "electronic"}


def test_tracks_with_no_genre_are_counted_separately(db, identity):
    add_track(db, "t1", tags=genre_tag("Ambient"))
    add_track(db, "t2", path="b.mp3", tags=None)

    tally = library.genre_tally(identity)

    assert tally["genres"] == [{"genre": "Ambient", "tracks": 1}]
    assert tally["untagged"] == 1


def test_genre_tally_is_private_to_the_account(db, identity, kelly):
    add_track(db, "t1", tags=genre_tag("Ambient"), library_id=1)
    add_track(db, "t2", path="b.mp3", tags=genre_tag("Rock"), library_id=2)

    assert library.genre_tally(identity)["genres"] == [
        {"genre": "Ambient", "tracks": 1}]
    assert library.genre_tally(kelly)["genres"] == [
        {"genre": "Rock", "tracks": 1}]


def test_an_unreadable_database_is_reported_not_raised_for_genres(
        identity, monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "navidrome_db", tmp_path / "nothing.db")

    tally = library.genre_tally(identity)

    assert tally["available"] is False
    assert tally["genres"] == []
    assert tally["reason"]


# --- what counts as an album folder (CODE_REVIEW M7) -----------------------
# Parts were counted before resolving, so "Artist/." passed as two and
# resolved to the artist directory - and a retag applies to everything under
# it. A folder in the quarantine passed too.

@pytest.mark.parametrize("folder", ["Radiohead/.", "Radiohead/Kid A/..",
                                    "./Radiohead"])
def test_a_dot_never_reaches_an_artist_directory(tmp_path, identity, folder):
    (tmp_path / "music" / "Radiohead" / "Kid A").mkdir(parents=True)
    with pytest.raises(ValueError):
        library.album_dir(identity, 1, folder)


def test_a_quarantined_folder_is_not_an_album(tmp_path, identity):
    (tmp_path / "music" / "duplicates-removed" / "Radiohead").mkdir(parents=True)
    with pytest.raises(ValueError, match="set aside"):
        library.album_dir(identity, 1, "duplicates-removed/Radiohead")


def test_a_quarantined_track_is_not_editable(tmp_path, identity):
    folder = tmp_path / "music" / "duplicates-removed" / "A" / "B"
    folder.mkdir(parents=True)
    (folder / "song.mp3").write_bytes(b"x")
    with pytest.raises(ValueError, match="set aside"):
        library.track_path(identity, 1, "duplicates-removed/A/B/song.mp3")


def test_measuring_may_reach_a_disc_folder_editing_may_not(tmp_path, identity):
    (tmp_path / "music" / "Artist" / "Album" / "CD1").mkdir(parents=True)

    with pytest.raises(ValueError):
        library.album_dir(identity, 1, "Artist/Album/CD1")
    assert library.album_dir(identity, 1, "Artist/Album/CD1",
                             any_depth=True).name == "CD1"


def test_an_opened_album_says_its_own_artist_not_a_tracks(db, identity):
    """An album opened from a song in search results only knew the track
    artist, and Edit details saved it as every file's album artist
    (CODE_REVIEW M8). The album's own names come back with its tracks."""
    add_track(db, "a", path="Artist/Record/1.mp3", album="Record",
              album_artist="Artist", artist="Artist feat. Guest")
    add_track(db, "b", path="Artist/Record/2.mp3", album="Record",
              album_artist="Artist", artist="Artist")

    opened = library.tracks(identity, 1, "Artist/Record")

    assert (opened["artist"], opened["album"]) == ("Artist", "Record")


def test_a_track_says_whether_it_has_an_album_artist(db, identity):
    """Without one the track artist decides the folder, so the page has to
    ask before changing it (CODE_REVIEW M9)."""
    add_track(db, "a", path="X/Y/1.mp3", album="Y", album_artist="",
              artist="Someone")
    add_track(db, "b", path="X/Y/2.mp3", album="Y", album_artist="X",
              artist="Someone")

    items = library.tracks(identity, 1, "X/Y")["items"]

    assert {t["id"]: t["has_albumartist"] for t in items} == {"a": False, "b": True}


# --- the merge search stays in one library (L23) -------------------------------

def test_the_listing_narrows_to_one_library(db, identity):
    """The album editor's merge search spanned every library, and picking
    another library's album only made a new album here with its name."""
    both = navidrome.Identity(
        user_id=identity.user_id, username=identity.username,
        is_admin=identity.is_admin, token="t", subsonic_token="st",
        subsonic_salt="ss",
        libraries=identity.libraries + [{"id": 2, "name": "Kelly", "path": "/k"}])
    make_album(db, "The Beatles/Abbey Road", 2, album="Abbey Road",
               album_artist="The Beatles")
    make_album(db, "The Beatles/Abbey Road (Kelly)", 2, library_id=2,
               album="Abbey Road", album_artist="The Beatles",
               album_id="al-kelly")

    every = library.listing(both, search="abbey")["albums"]
    mine = library.listing(both, search="abbey", library_id=1)["albums"]

    assert sorted(a["library_id"] for a in every) == [1, 2]
    assert [a["library_id"] for a in mine] == [1]


def test_the_merge_search_asks_for_the_albums_own_library():
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "app" / "static" / "js"
          / "library.js").read_text(encoding="utf-8")
    assert "&library_id=${album.library_id}&limit=8" in js
