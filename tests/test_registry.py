"""Album membership as a lookup instead of a guess.

Every test here is one of the identity bugs the registry exists to make
impossible. They were not hypothetical: one UUID landed on 745 files spanning
101 albums, and nine arriving tracks outvoted the one already filed and split
two records in half.
"""

from __future__ import annotations

import pytest

from app import registry


# --- what counts as the same album -----------------------------------------

def test_case_and_surrounding_space_do_not_make_a_new_album():
    assert (registry.album_key("The Beatles", "Abbey Road")
            == registry.album_key("the beatles", "  ABBEY ROAD "))


def test_punctuation_does_not_make_a_new_album():
    assert (registry.album_key("Beyoncé", "Don't Hurt Yourself")
            == registry.album_key("Beyoncé", "Dont Hurt Yourself"))


def test_a_typographic_apostrophe_is_the_same_album():
    """Spotify writes U+2019 and a hand-typed tag writes the plain one, so
    this is the difference that actually turns up on disk."""
    assert (registry.album_key("Guns N’ Roses", "Appetite for Destruction")
            == registry.album_key("Guns N' Roses", "Appetite for Destruction"))


def test_full_width_characters_are_the_same_letters():
    """Doujin releases tag titles in full-width Latin. They are the same
    album as the half-width spelling, typed on a different keyboard."""
    assert (registry.album_key("Artist", "ＴＯＨＯ")
            == registry.album_key("Artist", "TOHO"))


def test_an_edition_is_a_different_album():
    """The deliberate reversal of `staging.album_key`, which stripped these.
    Spotify presents them as two albums with two ids, and merging them puts
    two track 1s inside one record."""
    assert (registry.album_key("The Beatles", "Abbey Road")
            != registry.album_key("The Beatles",
                                  "Abbey Road (Super Deluxe Edition)"))


def test_the_same_album_title_by_two_artists_is_two_albums():
    assert (registry.album_key("Weezer", "Weezer")
            != registry.album_key("The Beatles", "Weezer"))


def test_the_artist_and_album_cannot_bleed_into_each_other():
    """A key is two fields joined, so a separator a tag could contain would
    let "A B"/"C" collide with "A"/"B C"."""
    assert (registry.album_key("A B", "C") != registry.album_key("A", "B C"))


# --- minting and looking up -------------------------------------------------

def test_the_first_track_mints_a_uuid_and_the_rest_find_it(state_db):
    first = registry.album_uuid_for(1, "The Beatles", "Abbey Road")
    second = registry.album_uuid_for(1, "The Beatles", "Abbey Road")
    assert first == second


def test_a_track_arriving_months_later_joins_the_same_album(state_db):
    """The bug this replaces: downloading track five of an album already in
    the library gave it a fresh UUID, so it arrived as a second, one-track
    copy of that record."""
    original = registry.album_uuid_for(1, "Mk.gee", "A Museum of Contradiction")
    latecomer = registry.album_uuid_for(1, "mk.gee",
                                        "A Museum of Contradiction ")
    assert latecomer == original


def test_two_albums_do_not_share_a_uuid(state_db):
    """745 files across 101 albums got one UUID, because the old assignment
    keyed on the directory rather than on the album."""
    one = registry.album_uuid_for(1, "The Beatles", "Abbey Road")
    other = registry.album_uuid_for(1, "The Beatles", "Revolver")
    assert one != other


def test_the_same_album_in_two_libraries_gets_two_uuids(state_db):
    """A UUID identifies a file, not a recording. Alex's copy and Kelly's
    copy are different files."""
    alex = registry.album_uuid_for(1, "The Beatles", "Abbey Road")
    kelly = registry.album_uuid_for(2, "The Beatles", "Abbey Road")
    assert alex != kelly


def test_known_does_not_mint(state_db):
    key = registry.album_key("The Beatles", "Abbey Road")
    assert registry.known(1, key) is None
    assert registry.count() == 0


def test_a_uuid_on_disk_is_adopted_rather_than_replaced(state_db):
    """A file already in the library arrives carrying its own album UUID.
    Inventing a second one beside it would split the record in two."""
    key = registry.album_key("The Beatles", "Abbey Road")
    existing = "11111111-1111-4111-8111-111111111111"
    assert registry.adopt(1, key, existing) == existing
    assert registry.album_uuid_for(1, "The Beatles", "Abbey Road") == existing


def test_adoption_never_overrules_a_registered_album(state_db):
    key = registry.album_key("The Beatles", "Abbey Road")
    registered = registry.album_uuid_for(1, "The Beatles", "Abbey Road")
    assert registry.adopt(1, key, "22222222-2222-4222-8222-222222222222") \
        == registered


def test_forgetting_an_album_lets_a_fresh_uuid_be_minted(state_db):
    key = registry.album_key("The Beatles", "Abbey Road")
    first = registry.uuid_for_key(1, key)
    assert registry.forget(1, key) is True
    assert registry.uuid_for_key(1, key) != first


def test_counting_is_per_library_or_overall(state_db):
    registry.album_uuid_for(1, "The Beatles", "Abbey Road")
    registry.album_uuid_for(1, "The Beatles", "Revolver")
    registry.album_uuid_for(2, "The Beatles", "Abbey Road")
    assert registry.count(1) == 2
    assert registry.count(2) == 1
    assert registry.count() == 3


def test_concurrent_first_tracks_agree_on_one_uuid(state_db):
    """Tracks of one album are downloaded at once - that is what the worker
    does. Two threads that both miss and both mint would hand one record two
    UUIDs, which is the split this table exists to prevent."""
    from concurrent.futures import ThreadPoolExecutor

    key = registry.album_key("The Beatles", "Abbey Road")
    with ThreadPoolExecutor(max_workers=8) as pool:
        got = list(pool.map(lambda _: registry.uuid_for_key(1, key), range(24)))

    assert len(set(got)) == 1


# --- retagging --------------------------------------------------------------

def test_a_retag_keeps_the_album_uuid(state_db):
    """The album keeps its Navidrome identity, so album-level stars and play
    counts survive and no file needs its UUID rewritten."""
    before = registry.album_uuid_for(1, "Unknown Artist", "Unknown Album")
    old = registry.album_key("Unknown Artist", "Unknown Album")
    new = registry.album_key("The Beatles", "Abbey Road")

    assert registry.repoint(1, old, new) == before
    assert registry.album_uuid_for(1, "The Beatles", "Abbey Road") == before


def test_a_retag_drops_the_key_it_came_from(state_db):
    old = registry.album_key("Unkown Beatles", "Abbey Road")
    new = registry.album_key("The Beatles", "Abbey Road")
    registry.uuid_for_key(1, old)

    registry.repoint(1, old, new)

    assert registry.known(1, old) is None
    assert registry.count(1) == 1


def test_the_album_already_in_the_library_wins(state_db):
    """Only the newcomer can be rewritten, so choosing its value would not
    move the established album - it would split it."""
    incumbent = registry.album_uuid_for(1, "The Beatles", "Abbey Road")
    old = registry.album_key("The Beatels", "Abbey Road")
    registry.uuid_for_key(1, old)

    settled = registry.repoint(1, old, registry.album_key("The Beatles",
                                                          "Abbey Road"))

    assert settled == incumbent
    assert registry.known(1, old) is None


def test_repointing_to_the_same_key_changes_nothing(state_db):
    key = registry.album_key("The Beatles", "Abbey Road")
    before = registry.uuid_for_key(1, key)
    assert registry.repoint(1, key, key) == before
    assert registry.count(1) == 1


def test_repointing_an_unknown_key_registers_the_destination(state_db):
    new = registry.album_key("The Beatles", "Abbey Road")
    settled = registry.repoint(1, registry.album_key("x", "y"), new)
    assert registry.known(1, new) == settled


def test_a_retag_in_one_library_leaves_the_other_alone(state_db):
    old = registry.album_key("Unknown Artist", "Unknown Album")
    new = registry.album_key("The Beatles", "Abbey Road")
    registry.uuid_for_key(1, old)
    kelly = registry.uuid_for_key(2, old)

    registry.repoint(1, old, new)

    assert registry.known(2, old) == kelly


def test_nothing_works_without_a_database():
    """The registry is the one place album membership is written down. A
    caller that reaches it before state.db is connected must fail loudly
    rather than mint UUIDs nothing will remember."""
    with pytest.raises(AssertionError):
        registry.album_uuid_for(1, "The Beatles", "Abbey Road")


# --- names made entirely of punctuation ------------------------------------
#
# Normalisation maps every non-word character to a space, so these used to
# reduce to "" and share a single key. Four Ed Sheeran albums became one
# record with four track 1s in it, which is the exact failure this table
# exists to prevent.

def test_ed_sheerans_four_albums_are_four_albums():
    keys = {registry.album_key("Ed Sheeran", name)
            for name in ("+", "-", "=", "÷")}
    assert len(keys) == 4


def test_a_punctuation_only_name_does_not_vanish():
    assert registry.normalize("+") == "+"
    assert registry.normalize("!!!") == "!!!"
    assert registry.normalize("...") == "..."


def test_a_band_called_out_of_punctuation_keeps_its_name():
    assert (registry.album_key("!!!", "Strange Weather, Isn't It?")
            != registry.album_key("...", "Strange Weather, Isn't It?"))


def test_spacing_still_does_not_matter_in_a_punctuation_name():
    """The ordinary rule still applies - it is only the fallback that is
    different, not the principle."""
    assert registry.normalize("+ +") == registry.normalize("++")


def test_a_genuinely_empty_name_is_still_empty():
    assert registry.normalize("") == ""
    assert registry.normalize("   ") == ""


def test_the_punctuation_fallback_mints_separate_uuids(state_db):
    plus = registry.album_uuid_for(1, "Ed Sheeran", "+")
    minus = registry.album_uuid_for(1, "Ed Sheeran", "-")
    assert plus != minus
    assert registry.count(1) == 2
