"""Smart playlists - the half of playlists Navidrome will not edit itself.

Navidrome happily renames an ordinary playlist and reorders its tracks, but
a smart playlist's rules can only be written as a `.nsp` file dropped on
disk or posted to its API. That is the gap this fills, and the reason this
module does not touch ordinary playlists at all: duplicating what Navidrome
already does well would only be a worse version of it.

Rules are stored in Navidrome's own shape, which nests the operator around
the field:

    {"all": [{"gt": {"rating": 4}}], "sort": "-dateloved", "limit": 100}

That is awkward to build a form against - the operator is the key, the field
is the key inside it, and the sort direction is a character on the front of
a string. So the browser never sees it. It works in a flat shape, one
condition per row, and the translation lives here in one place:

    {"match": "all",
     "conditions": [{"field": "rating", "operator": "gt", "value": 4}],
     "sort": "dateloved", "direction": "desc", "limit": 100}

Anything that does not fit that flat shape - a nested `any` inside an `all`,
say, written by hand in a `.nsp` file - is handed back marked unsupported
rather than flattened. Editing such a playlist here would silently discard
the part the form cannot show, and a smart playlist quietly matching the
wrong thing is worse than one this app declines to open.

Nothing is written to Navidrome's database. Every change goes through the
API as the person signed in, because a smart playlist's rules evaluate
against that person's own stars, ratings and play counts: the same rules
saved by somebody else would match a different set of songs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import navidrome

log = logging.getLogger("navidrome_companion.playlists")


@dataclass(frozen=True)
class Field:
    """One thing a condition can be about."""

    name: str
    label: str
    kind: str
    hint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "label": self.label, "kind": self.kind,
                "hint": self.hint, "operators": OPERATORS[self.kind]}


# What each kind of field can be asked. Navidrome accepts more operators
# than these, but only where they mean something: `startsWith` on a play
# count parses and then never matches anything, and an operator that cannot
# match is worse than one that is missing, because it looks like it worked.
OPERATORS: dict[str, list[dict[str, str]]] = {
    "text": [
        {"name": "contains", "label": "contains"},
        {"name": "notContains", "label": "does not contain"},
        {"name": "is", "label": "is exactly"},
        {"name": "isNot", "label": "is not"},
        {"name": "startsWith", "label": "starts with"},
        {"name": "endsWith", "label": "ends with"},
    ],
    "number": [
        {"name": "gt", "label": "is more than"},
        {"name": "lt", "label": "is less than"},
        {"name": "is", "label": "is"},
        {"name": "isNot", "label": "is not"},
    ],
    "boolean": [
        {"name": "is", "label": "is"},
    ],
    "date": [
        {"name": "inTheLast", "label": "in the last"},
        {"name": "notInTheLast", "label": "not in the last"},
        {"name": "before", "label": "before"},
        {"name": "after", "label": "after"},
    ],
}


FIELDS: list[Field] = [
    Field("title", "Title", "text"),
    Field("album", "Album", "text"),
    Field("artist", "Artist", "text"),
    Field("albumartist", "Album artist", "text"),
    Field("genre", "Genre", "text"),
    Field("comment", "Comment", "text"),
    Field("filepath", "File path", "text"),
    Field("filetype", "File type", "text", "mp3, flac, m4a"),

    Field("year", "Year", "number"),
    Field("rating", "Rating", "number", "0 to 5"),
    Field("playcount", "Play count", "number"),
    Field("duration", "Duration", "number", "seconds"),
    Field("bitrate", "Bitrate", "number", "kbps"),
    Field("bpm", "BPM", "number"),
    Field("tracknumber", "Track number", "number"),
    Field("discnumber", "Disc number", "number"),

    Field("loved", "Starred", "boolean"),
    Field("compilation", "Compilation", "boolean"),

    Field("dateadded", "Date added", "date"),
    Field("lastplayed", "Last played", "date"),
    Field("dateloved", "Date starred", "date"),
]

_BY_NAME = {f.name: f for f in FIELDS}

# Which libraries a playlist draws from is not a condition row. Navidrome
# evaluates a smart playlist against every track on the server - it checks
# who owns the playlist and never which libraries they may see - so Kelly's
# "play count > -1" collected Alex's 7,000 tracks as well as her own 437.
# Leaving that to a row somebody has to remember to add is how it happened,
# so the scope is a property of the playlist instead, and every save writes
# it. `is` rather than the `contains` the first hand-written playlists used:
# `contains` is a LIKE, and "1" is a substring of 10, 11 and 21.
LIBRARY_FIELD = "library_id"

# `random` is a sort and nothing else - there is no such thing on a track to
# compare against - so it lives here rather than in FIELDS.
SORTS: list[dict[str, str]] = [
    {"name": "dateadded", "label": "Date added"},
    {"name": "dateloved", "label": "Date starred"},
    {"name": "lastplayed", "label": "Last played"},
    {"name": "playcount", "label": "Play count"},
    {"name": "rating", "label": "Rating"},
    {"name": "year", "label": "Year"},
    {"name": "title", "label": "Title"},
    {"name": "album", "label": "Album"},
    {"name": "artist", "label": "Artist"},
    {"name": "duration", "label": "Duration"},
    {"name": "random", "label": "Random"},
]

_SORT_NAMES = {s["name"] for s in SORTS}


def vocabulary(identity: navidrome.Identity) -> dict[str, Any]:
    """Everything the form needs to render itself.

    Sent from here rather than hard-coded in the browser so the two cannot
    drift: a field added below appears in the form without touching it.
    """
    return {
        "fields": [f.as_dict() for f in FIELDS],
        "sorts": SORTS,
        # Every rule is limited to these. A person with one library is never
        # asked; one with several can narrow a playlist to some of them.
        "libraries": [{"id": lib["id"], "name": lib["name"]}
                      for lib in identity.libraries],
    }


class Unsupported(Exception):
    """The rules on this playlist cannot be shown as a flat list of rows."""


def to_form(rules: dict[str, Any]) -> dict[str, Any]:
    """Navidrome's rules, as the form works in.

    Raises Unsupported rather than dropping anything it cannot represent.
    """
    if not isinstance(rules, dict):
        raise Unsupported("rules are not an object")

    match = next((key for key in ("all", "any") if key in rules), None)
    if match is None:
        raise Unsupported("rules have no 'all' or 'any' block")
    if "all" in rules and "any" in rules:
        raise Unsupported("rules combine 'all' and 'any' at the top level")

    entries = rules[match]
    if not isinstance(entries, list):
        raise Unsupported(f"the '{match}' block is not a list")

    # The library scope sits beside the conditions in an `all`. An `any`
    # playlist is saved as {"all": [{"any": [...]}, scope]}, because a scope
    # inside the `any` would be one more way to match rather than a limit.
    libraries = None
    if match == "all":
        rest = []
        for entry in entries:
            scope = _library_scope(entry)
            if scope is None:
                rest.append(entry)
            elif libraries is not None:
                raise Unsupported("it names its libraries twice")
            else:
                libraries = scope
        if (libraries is not None and len(rest) == 1
                and isinstance(rest[0], dict) and list(rest[0]) == ["any"]):
            match, rest = "any", rest[0]["any"]
            if not isinstance(rest, list):
                raise Unsupported("the 'any' block is not a list")
        entries = rest

    conditions = []
    for entry in entries:
        if not isinstance(entry, dict) or len(entry) != 1:
            raise Unsupported("a condition is not a single operator")
        operator, payload = next(iter(entry.items()))
        if operator in ("all", "any", "not"):
            raise Unsupported("conditions are nested")
        if not isinstance(payload, dict) or len(payload) != 1:
            raise Unsupported(f"the '{operator}' condition names no field")
        name, value = next(iter(payload.items()))
        if name == LIBRARY_FIELD:
            # Only reachable inside an `any`, where it widens rather than
            # limits, and the form has no way to say that.
            raise Unsupported("a library is one of its 'any' conditions")
        spec = _BY_NAME.get(name)
        if spec is None:
            raise Unsupported(f"unknown field '{name}'")
        # Checked here as well as on the way out. The form only offers the
        # operators a field can take, so a rule written elsewhere with, say,
        # `contains` on a play count opened as fully editable and then failed
        # on Save with an error about a rule this app had handed over itself.
        # Better to say up front that it cannot be shown.
        if operator not in {o["name"] for o in OPERATORS[spec.kind]}:
            raise Unsupported(
                f"'{spec.label}' cannot be asked '{operator}' in this editor")
        conditions.append({"field": name, "operator": operator,
                           "value": value})

    # Refused rather than dropped: the form cannot show these, and a Save
    # that quietly lost them changed which tracks the playlist holds.
    for key in ("offset", "limitPercent"):
        if rules.get(key):
            raise Unsupported(f"it sets '{key}', which this editor cannot show")

    sort = rules.get("sort") or ""
    # Navidrome takes the direction two ways, a leading '-' and a separate
    # `order`, and the two together cancel out. Reading only the '-' opened
    # a "most played" list as ascending and saved it as the least played.
    descending = sort.startswith("-") != (
        str(rules.get("order") or "").lower() == "desc")
    direction = "desc" if descending else "asc"
    sort = sort.lstrip("+-")
    if sort and sort not in _SORT_NAMES:
        raise Unsupported(f"it is sorted by '{sort}', which this editor "
                          "cannot show")

    return {
        "match": match,
        "conditions": conditions,
        "sort": sort,
        "direction": direction,
        "limit": rules.get("limit") or 0,
        # None means the rules name no library, and so match every track on
        # the server. Reported rather than filled in, so the card can say so.
        "libraries": libraries,
    }


def _library_scope(entry: Any) -> list[int] | None:
    """The library ids a top-level condition limits to, if it is one.

    Accepts the `contains` form the first playlists were written with, and a
    string id, because that is what those playlists hold. Saving one again
    writes it back as `is`.
    """
    if not isinstance(entry, dict) or len(entry) != 1:
        return None
    operator, payload = next(iter(entry.items()))
    if operator not in ("is", "contains") or not isinstance(payload, dict):
        return None
    if list(payload) != [LIBRARY_FIELD]:
        return None
    value = payload[LIBRARY_FIELD]
    values = value if isinstance(value, list) else [value]
    try:
        return sorted({int(v) for v in values})
    except (TypeError, ValueError):
        raise Unsupported(f"the library {value!r} is not an id") from None


def to_rules(form: dict[str, Any], allowed: list[int]) -> dict[str, Any]:
    """The form, as Navidrome's rules, limited to libraries in `allowed`.

    `allowed` is required, not defaulted: a rule saved without a library
    scope matches every library on the server, and that was the bug.
    The form may narrow it to some of them; it may not reach past it.

    Validated here rather than trusted: the browser builds these from
    dropdowns, but the endpoint is reachable without one.
    """
    if not allowed:
        raise ValueError(
            "This account can see no libraries, so a playlist would have "
            "nothing to draw from.")
    chosen = form.get("libraries")
    if not chosen:
        libraries = sorted(set(allowed))
    else:
        try:
            libraries = sorted({int(i) for i in chosen})
        except (TypeError, ValueError):
            raise ValueError("Libraries must be ids.") from None
        stray = [i for i in libraries if i not in allowed]
        if stray:
            raise ValueError(
                f"Library {stray[0]} is not one this account can see.")

    match = form.get("match", "all")
    if match not in ("all", "any"):
        raise ValueError("match must be 'all' or 'any'")

    conditions = form.get("conditions") or []
    if not conditions:
        raise ValueError(
            "A smart playlist needs at least one condition. Without one it "
            "would match the whole library.")

    built = []
    for row in conditions:
        name = row.get("field")
        spec = _BY_NAME.get(name)
        if spec is None:
            raise ValueError(f"There is no field called {name!r}.")
        operator = row.get("operator")
        if operator not in {o["name"] for o in OPERATORS[spec.kind]}:
            raise ValueError(
                f"{spec.label} cannot be asked {operator!r}.")
        value = _coerce(spec, row.get("value"), operator)
        # An empty value is not a filter. "Title contains ''" matches every
        # track in the library, which is the same accident as saving with no
        # conditions at all - and that is already refused, so refuse this
        # for the same reason rather than quietly building a playlist of
        # everything.
        if value == "":
            raise ValueError(
                f"{spec.label} has no value, so it would match every track. "
                "Fill it in or remove the condition.")
        built.append({operator: {name: value}})

    # A list is `library_id IN (...)` to Navidrome; one id stays a plain
    # value so the common case reads the way a hand-written rule would.
    scope = {"is": {LIBRARY_FIELD: libraries[0] if len(libraries) == 1
                    else libraries}}
    if match == "all":
        rules: dict[str, Any] = {"all": built + [scope]}
    else:
        rules = {"all": [{"any": built}, scope]}

    sort = form.get("sort") or ""
    if sort:
        if sort not in _SORT_NAMES:
            raise ValueError(f"Cannot sort by {sort!r}.")
        rules["sort"] = ("-" if form.get("direction") == "desc" else "") + sort

    limit = form.get("limit") or 0
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise ValueError("The limit must be a whole number.") from None
    if limit < 0:
        raise ValueError("The limit cannot be negative.")
    if limit:
        rules["limit"] = limit

    return rules


# Date operators whose value is a number of days rather than a date.
DAY_OPERATORS = {"inTheLast", "notInTheLast"}


def _coerce(spec: Field, value: Any, operator: str = "") -> Any:
    """Put a value in the shape Navidrome expects for that kind of field.

    The browser sends strings for everything, and a rating compared against
    the string "4" matches nothing while looking perfectly correct.
    """
    if spec.kind == "boolean":
        return value in (True, "true", "yes", "1", 1)
    if spec.kind == "number":
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(
                f"{spec.label} needs a number, not {value!r}.") from None
        return int(number) if number.is_integer() else number
    if spec.kind == "date":
        # Both forms are strings to Navidrome: a day count for inTheLast, a
        # date for before/after. Checked against the operator, because the
        # editor once sent a date as the day count and this passed it on.
        text = str(value or "").strip()
        if operator in DAY_OPERATORS:
            if not text.isdigit() or int(text) < 1:
                raise ValueError(
                    f"{spec.label} needs a number of days, not {value!r}.")
        else:
            try:
                datetime.strptime(text, "%Y-%m-%d")
            except ValueError:
                raise ValueError(
                    f"{spec.label} needs a date, not {value!r}.") from None
        return text
    return str(value if value is not None else "")


def mine(identity: navidrome.Identity) -> list[dict[str, Any]]:
    """This person's smart playlists, newest change first.

    Only theirs, and only smart ones. Navidrome will list playlists shared
    by other people, and offering to edit one would either fail on their
    server or - worse - succeed and change what somebody else is listening
    to.
    """
    found = []
    for raw in navidrome.playlists(identity) or []:
        if not _is_mine(raw, identity):
            continue
        rules = raw.get("rules")
        if not rules:
            continue
        entry = {
            "id": raw.get("id"),
            "name": raw.get("name") or "",
            "comment": raw.get("comment") or "",
            "public": bool(raw.get("public")),
            "song_count": raw.get("songCount") or 0,
            "duration": raw.get("duration") or 0,
            "updated_at": raw.get("updatedAt") or "",
            "evaluated_at": raw.get("evaluatedAt") or "",
        }
        try:
            entry["form"] = to_form(rules)
        except Unsupported as exc:
            # Shown, and shown as uneditable. Hiding it would look like the
            # playlist had gone missing.
            entry["form"] = None
            entry["unsupported"] = str(exc)
        found.append(entry)

    found.sort(key=lambda p: p["updated_at"], reverse=True)
    return found


def _is_mine(raw: dict[str, Any], identity: navidrome.Identity) -> bool:
    """Whether a playlist from the API belongs to whoever is asking.

    Navidrome lists playlists other people have shared, and offering to edit
    one would change what somebody else is listening to. Both the id and the
    name are checked because the field this arrives under has moved between
    versions, and a filter that silently matches nothing would present an
    empty page as "you have no smart playlists" - a wrong answer that looks
    like a right one.
    """
    owner_id = raw.get("ownerId") or raw.get("owner_id") or ""
    if owner_id:
        return str(owner_id) == identity.user_id
    owner_name = raw.get("ownerName") or raw.get("owner") or ""
    if owner_name:
        return str(owner_name) == identity.username
    # Neither field present: this is not something to guess at, because
    # guessing wrong means editing another person's playlist.
    log.warning("playlist %r names no owner; leaving it alone",
                raw.get("name"))
    return False


def save(identity: navidrome.Identity, name: str, form: dict[str, Any],
         comment: str = "", public: bool = False,
         playlist_id: str | None = None) -> dict[str, Any]:
    """Create or update one smart playlist, owned by whoever is signed in."""
    name = (name or "").strip()
    if not name:
        raise ValueError("A playlist needs a name.")

    body = {
        "name": name,
        "comment": comment or "",
        "public": bool(public),
        "rules": to_rules(form, [lib["id"] for lib in identity.libraries]),
    }
    saved = navidrome.save_playlist(identity, body, playlist_id)

    # The id is what tells the editor it is now editing rather than still
    # creating, so a create that cannot report one turns the next Save into
    # a second playlist. Navidrome does return it; this is the fallback for
    # when it does not, because the cost of being wrong is silent and the
    # cost of asking again is one request.
    new_id = saved.get("id") or playlist_id
    if not new_id:
        log.warning("Navidrome returned no id for %r; looking it up", name)
        matches = [p for p in mine(identity) if p["name"] == name]
        if matches:
            new_id = matches[0]["id"]

    log.info("%s saved smart playlist %r", identity.username, name)
    return {
        "id": new_id,
        "name": saved.get("name") or name,
        # Navidrome evaluates the rules on save, so the count it returns is
        # the answer to "what does this match?" - computed by the thing that
        # will be answering that question from now on, rather than by a
        # second implementation here that could disagree with it.
        "song_count": saved.get("songCount") or 0,
    }


def remove(identity: navidrome.Identity, playlist_id: str) -> None:
    """Delete one, after checking it is this person's and is smart.

    The id comes from the browser, and the API would just as happily delete
    an ordinary playlist full of hand-picked tracks.
    """
    if not any(p["id"] == playlist_id for p in mine(identity)):
        raise ValueError("That is not one of your smart playlists.")
    navidrome.delete_playlist(identity, playlist_id)
    log.info("%s deleted smart playlist %s", identity.username, playlist_id)
