"""Translating between Navidrome's rule shape and the form's flat one.

The plan lists this first among the tests worth having, because the
translation is lossy in exactly one direction and a smart playlist that
quietly matches the wrong thing gives no sign of it.
"""

from __future__ import annotations

import pytest

from app import playlists


def test_a_rule_survives_the_round_trip():
    rules = {
        "all": [
            {"contains": {"artist": "Burial"}},
            {"gt": {"rating": 3}},
            {"is": {"loved": True}},
        ],
        "sort": "-dateloved",
        "limit": 100,
    }
    assert playlists.to_rules(playlists.to_form(rules)) == rules


def test_an_ascending_sort_round_trips_without_a_sign():
    rules = {"any": [{"is": {"year": 1997}}], "sort": "year"}
    assert playlists.to_rules(playlists.to_form(rules)) == rules


def test_no_limit_stays_absent_rather_than_becoming_zero():
    """`limit: 0` is not the same as no limit to Navidrome."""
    rules = {"all": [{"contains": {"album": "Untrue"}}]}
    assert "limit" not in playlists.to_rules(playlists.to_form(rules))


# --- what it refuses to show ----------------------------------------------

@pytest.mark.parametrize("rules, why", [
    ({"sort": "year"}, "no all or any block"),
    ({"all": [], "any": []}, "both at the top level"),
    ({"all": [{"any": [{"is": {"year": 1997}}]}]}, "nested"),
    ({"all": [{"contains": {"artist": "a", "album": "b"}}]}, "two fields"),
    ({"all": [{"contains": {"nosuchfield": "x"}}]}, "unknown field"),
    ({"all": [{"contains": {"playcount": 3}}]}, "operator the field cannot take"),
])
def test_rules_it_cannot_represent_are_refused_not_flattened(rules, why):
    """Flattening would silently discard the part the form cannot show, and
    the playlist would then be saved back without it."""
    with pytest.raises(playlists.Unsupported):
        playlists.to_form(rules)


def test_an_unshowable_rule_is_refused_on_the_way_in_too():
    """It used to open as fully editable and fail only on Save, with an error
    naming a rule this app had itself handed to the form."""
    rules = {"all": [{"contains": {"playcount": 3}}]}
    with pytest.raises(playlists.Unsupported, match="cannot be asked"):
        playlists.to_form(rules)


# --- validation on the way out ---------------------------------------------

def test_a_playlist_with_no_conditions_is_refused():
    """It would match the whole library."""
    with pytest.raises(ValueError, match="at least one condition"):
        playlists.to_rules({"match": "all", "conditions": []})


def test_an_empty_value_is_refused():
    """"Title contains ''" matches every track, which is the same accident
    as saving with no conditions at all."""
    with pytest.raises(ValueError, match="no value"):
        playlists.to_rules({"conditions": [
            {"field": "title", "operator": "contains", "value": ""}]})


def test_an_unknown_field_is_refused():
    with pytest.raises(ValueError, match="no field called"):
        playlists.to_rules({"conditions": [
            {"field": "nope", "operator": "is", "value": "x"}]})


def test_an_operator_a_field_cannot_take_is_refused():
    with pytest.raises(ValueError, match="cannot be asked"):
        playlists.to_rules({"conditions": [
            {"field": "rating", "operator": "contains", "value": "4"}]})


def test_a_negative_limit_is_refused():
    with pytest.raises(ValueError, match="cannot be negative"):
        playlists.to_rules({
            "conditions": [{"field": "title", "operator": "is", "value": "x"}],
            "limit": -1})


def test_an_unknown_sort_is_refused():
    with pytest.raises(ValueError, match="Cannot sort by"):
        playlists.to_rules({
            "conditions": [{"field": "title", "operator": "is", "value": "x"}],
            "sort": "loudness"})


def test_a_bad_match_mode_is_refused():
    with pytest.raises(ValueError, match="must be 'all' or 'any'"):
        playlists.to_rules({"match": "some", "conditions": [
            {"field": "title", "operator": "is", "value": "x"}]})


# --- coercion ---------------------------------------------------------------

def test_a_number_is_sent_as_a_number():
    """The browser sends strings for everything, and a rating compared
    against the string "4" matches nothing while looking correct."""
    rules = playlists.to_rules({"conditions": [
        {"field": "rating", "operator": "gt", "value": "4"}]})
    assert rules["all"] == [{"gt": {"rating": 4}}]


def test_a_zero_is_a_value_not_an_empty_one():
    """`0 == ""` is False in Python, but this is worth pinning: a play count
    of zero is a real filter and must not trip the empty-value refusal."""
    rules = playlists.to_rules({"conditions": [
        {"field": "playcount", "operator": "is", "value": "0"}]})
    assert rules["all"] == [{"is": {"playcount": 0}}]


def test_a_false_boolean_is_a_value_not_an_empty_one():
    rules = playlists.to_rules({"conditions": [
        {"field": "loved", "operator": "is", "value": "false"}]})
    assert rules["all"] == [{"is": {"loved": False}}]


def test_a_non_numeric_value_for_a_number_field_says_so():
    with pytest.raises(ValueError, match="needs a number"):
        playlists.to_rules({"conditions": [
            {"field": "year", "operator": "is", "value": "nineteen"}]})


# --- ownership --------------------------------------------------------------

def _identity():
    from app.navidrome import Identity
    return Identity(user_id="u-alex", username="alex", is_admin=False,
                    token="t", subsonic_token="s", subsonic_salt="s")


@pytest.mark.parametrize("raw, mine", [
    ({"ownerId": "u-alex"}, True),
    ({"ownerId": "u-kelly"}, False),
    ({"ownerName": "alex"}, True),
    ({"ownerName": "kelly"}, False),
    ({}, False),
])
def test_only_your_own_playlists_are_offered_for_editing(raw, mine):
    """Navidrome lists playlists other people have shared. Offering to edit
    one would change what somebody else is listening to, and a playlist
    naming no owner is not something to guess at."""
    assert playlists._is_mine(raw, _identity()) is mine
