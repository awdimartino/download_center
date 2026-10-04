"""Translating between Navidrome's rule shape and the form's flat one.

The plan lists this first among the tests worth having, because the
translation is lossy in exactly one direction and a smart playlist that
quietly matches the wrong thing gives no sign of it.
"""

from __future__ import annotations

import pytest

from app import playlists

SCOPE = {"is": {"library_id": 1}}


def _rules(form):
    return playlists.to_rules(form, [1])


def test_a_rule_survives_the_round_trip():
    rules = {
        "all": [
            {"contains": {"artist": "Burial"}},
            {"gt": {"rating": 3}},
            {"is": {"loved": True}},
            SCOPE,
        ],
        "sort": "-dateloved",
        "limit": 100,
    }
    assert _rules(playlists.to_form(rules)) == rules


def test_an_ascending_sort_round_trips_without_a_sign():
    rules = {"all": [{"any": [{"is": {"year": 1997}}]}, SCOPE], "sort": "year"}
    assert _rules(playlists.to_form(rules)) == rules


def test_no_limit_stays_absent_rather_than_becoming_zero():
    """`limit: 0` is not the same as no limit to Navidrome."""
    rules = {"all": [{"contains": {"album": "Untrue"}}]}
    assert "limit" not in _rules(playlists.to_form(rules))


# --- which libraries it draws from -----------------------------------------
#
# Navidrome evaluates a smart playlist against every track on the server,
# whoever owns it. Kelly's "play count > -1" matched ~7,000 tracks against
# her library's 437.

def test_every_saved_rule_is_limited_to_the_accounts_libraries():
    rules = playlists.to_rules({"conditions": [
        {"field": "playcount", "operator": "gt", "value": "-1"}]}, [2])
    assert rules["all"] == [{"gt": {"playcount": -1}},
                            {"is": {"library_id": 2}}]


def test_an_any_rule_is_limited_from_outside_the_any():
    """Inside the `any`, the library would be one more way to match."""
    rules = playlists.to_rules({"match": "any", "conditions": [
        {"field": "rating", "operator": "gt", "value": "4"},
        {"field": "loved", "operator": "is", "value": "true"}]}, [2])
    assert rules["all"] == [
        {"any": [{"gt": {"rating": 4}}, {"is": {"loved": True}}]},
        {"is": {"library_id": 2}}]
    form = playlists.to_form(rules)
    assert form["match"] == "any" and form["libraries"] == [2]
    assert len(form["conditions"]) == 2


def test_several_libraries_are_one_condition():
    """A list is `library_id IN (...)` to Navidrome."""
    rules = playlists.to_rules({"conditions": [
        {"field": "rating", "operator": "gt", "value": "4"}]}, [5, 1])
    assert rules["all"][-1] == {"is": {"library_id": [1, 5]}}


def test_a_playlist_can_be_narrowed_to_some_of_them():
    rules = playlists.to_rules({"libraries": ["5"], "conditions": [
        {"field": "rating", "operator": "gt", "value": "4"}]}, [1, 5])
    assert rules["all"][-1] == {"is": {"library_id": 5}}


def test_a_library_the_account_cannot_see_is_refused():
    with pytest.raises(ValueError, match="not one this account can see"):
        playlists.to_rules({"libraries": [2], "conditions": [
            {"field": "rating", "operator": "gt", "value": "4"}]}, [1])


def test_an_account_with_no_libraries_cannot_save():
    with pytest.raises(ValueError, match="no libraries"):
        playlists.to_rules({"conditions": [
            {"field": "rating", "operator": "gt", "value": "4"}]}, [])


def test_an_unscoped_rule_says_so():
    form = playlists.to_form({"all": [{"gt": {"rating": 4}}]})
    assert form["libraries"] is None


def test_the_hand_written_contains_form_is_read_as_a_scope():
    """What the first playlists on this server hold. Saving writes `is`."""
    rules = {"all": [{"gt": {"playcount": -1}},
                     {"contains": {"library_id": "2"}}], "sort": "-artist"}
    form = playlists.to_form(rules)
    assert form["libraries"] == [2]
    assert form["conditions"] == [
        {"field": "playcount", "operator": "gt", "value": -1}]
    assert playlists.to_rules(form, [2])["all"][-1] == {"is": {"library_id": 2}}


# --- what it refuses to show ----------------------------------------------

@pytest.mark.parametrize("rules, why", [
    ({"sort": "year"}, "no all or any block"),
    ({"all": [], "any": []}, "both at the top level"),
    ({"all": [{"any": [{"is": {"year": 1997}}]}]}, "nested"),
    ({"all": [{"contains": {"artist": "a", "album": "b"}}]}, "two fields"),
    ({"all": [{"contains": {"nosuchfield": "x"}}]}, "unknown field"),
    ({"all": [{"contains": {"playcount": 3}}]}, "operator the field cannot take"),
    ({"any": [{"gt": {"rating": 4}}, {"is": {"library_id": 1}}]},
     "a library that widens an any"),
    ({"all": [{"is": {"library_id": 1}}, {"is": {"library_id": 2}}]},
     "two library scopes"),
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
        _rules({"match": "all", "conditions": []})


def test_an_empty_value_is_refused():
    """"Title contains ''" matches every track, which is the same accident
    as saving with no conditions at all."""
    with pytest.raises(ValueError, match="no value"):
        _rules({"conditions": [
            {"field": "title", "operator": "contains", "value": ""}]})


def test_an_unknown_field_is_refused():
    with pytest.raises(ValueError, match="no field called"):
        _rules({"conditions": [
            {"field": "nope", "operator": "is", "value": "x"}]})


def test_an_operator_a_field_cannot_take_is_refused():
    with pytest.raises(ValueError, match="cannot be asked"):
        _rules({"conditions": [
            {"field": "rating", "operator": "contains", "value": "4"}]})


def test_a_negative_limit_is_refused():
    with pytest.raises(ValueError, match="cannot be negative"):
        _rules({
            "conditions": [{"field": "title", "operator": "is", "value": "x"}],
            "limit": -1})


def test_an_unknown_sort_is_refused():
    with pytest.raises(ValueError, match="Cannot sort by"):
        _rules({
            "conditions": [{"field": "title", "operator": "is", "value": "x"}],
            "sort": "loudness"})


def test_a_bad_match_mode_is_refused():
    with pytest.raises(ValueError, match="must be 'all' or 'any'"):
        _rules({"match": "some", "conditions": [
            {"field": "title", "operator": "is", "value": "x"}]})


# --- coercion ---------------------------------------------------------------

def test_a_number_is_sent_as_a_number():
    """The browser sends strings for everything, and a rating compared
    against the string "4" matches nothing while looking correct."""
    rules = _rules({"conditions": [
        {"field": "rating", "operator": "gt", "value": "4"}]})
    assert rules["all"][0] == {"gt": {"rating": 4}}


def test_a_zero_is_a_value_not_an_empty_one():
    """`0 == ""` is False in Python, but this is worth pinning: a play count
    of zero is a real filter and must not trip the empty-value refusal."""
    rules = _rules({"conditions": [
        {"field": "playcount", "operator": "is", "value": "0"}]})
    assert rules["all"][0] == {"is": {"playcount": 0}}


def test_a_false_boolean_is_a_value_not_an_empty_one():
    rules = _rules({"conditions": [
        {"field": "loved", "operator": "is", "value": "false"}]})
    assert rules["all"][0] == {"is": {"loved": False}}


def test_a_non_numeric_value_for_a_number_field_says_so():
    with pytest.raises(ValueError, match="needs a number"):
        _rules({"conditions": [
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


# The editor once sent a date as the day count for "in the last", and this
# passed it to Navidrome untouched (CODE_REVIEW H8). The value has to fit
# its operator.

def _date_rule(operator, value):
    return _rules({"conditions": [
        {"field": "dateadded", "operator": operator, "value": value}]})


def test_in_the_last_takes_a_number_of_days():
    rules = _date_rule("inTheLast", "30")
    assert {"inTheLast": {"dateadded": "30"}} in rules["all"]


@pytest.mark.parametrize("operator", ["inTheLast", "notInTheLast"])
def test_in_the_last_refuses_a_date(operator):
    with pytest.raises(ValueError, match="number of days"):
        _date_rule(operator, "2026-09-01")


@pytest.mark.parametrize("operator", ["before", "after"])
def test_before_and_after_refuse_a_day_count(operator):
    with pytest.raises(ValueError, match="needs a date"):
        _date_rule(operator, "30")


# --- Navidrome unreachable while updating (L37) ---------------------------------

@pytest.mark.asyncio
async def test_updating_a_playlist_with_navidrome_down_is_a_502(monkeypatch):
    """The ownership check's own failure escaped as a 500."""
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app import main

    def down(identity):
        raise ConnectionRefusedError("Connection refused")

    monkeypatch.setattr(main.smart_playlists, "mine", down)
    with pytest.raises(HTTPException) as refused:
        await main.update_playlist(
            "p1", main.PlaylistRequest(name="x", form={}),
            SimpleNamespace(identity=None, id="s"))
    assert refused.value.status_code == 502
