"""Sessions, backed by Navidrome's own accounts.

There is no user table here and there should not be one. Navidrome already
knows who exists, what libraries each person may see and what they have
starred; a second copy of any of that would eventually disagree with the
first, and the disagreement would be invisible until it mattered.

So logging in means asking Navidrome, and a session is just the answer it
gave, held for as long as the browser keeps using it. Nothing is written to
disk: a restart signs everyone out, which is the correct trade for never
storing anyone's credentials or tokens at rest.

Everything the application does on someone's behalf - which library a
download lands in, whose stars a duplicate carries, who a playlist belongs
to - is decided by the session rather than by configuration. That is the
whole point: per-user state was being treated as a property of the files,
and it is not.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

from . import navidrome

log = logging.getLogger("download_center.auth")

COOKIE = "dc_session"
# Long enough not to interrupt an afternoon, short enough that a forgotten
# browser on a shared machine does not stay signed in indefinitely.
LIFETIME_SECONDS = 14 * 24 * 60 * 60


@dataclass
class Session:
    id: str
    identity: navidrome.Identity
    created_at: float
    last_seen: float

    @property
    def expired(self) -> bool:
        return time.time() - self.last_seen > LIFETIME_SECONDS

    def as_dict(self) -> dict[str, Any]:
        return {
            "username": self.identity.username,
            "is_admin": self.identity.is_admin,
            "libraries": self.identity.libraries,
        }


_sessions: dict[str, Session] = {}
_lock = threading.Lock()


def sign_in(username: str, password: str) -> Session:
    identity = navidrome.login(username, password)
    now = time.time()
    session = Session(secrets.token_urlsafe(32), identity, now, now)
    with _lock:
        _sessions[session.id] = session
    log.info("%s signed in (%d librar%s)", identity.username,
             len(identity.libraries),
             "y" if len(identity.libraries) == 1 else "ies")
    return session


def sign_out(session_id: str) -> None:
    with _lock:
        _sessions.pop(session_id, None)


def get(session_id: str | None) -> Session | None:
    if not session_id:
        return None
    with _lock:
        session = _sessions.get(session_id)
        if session is None:
            return None
        if session.expired:
            del _sessions[session.id]
            return None
        session.last_seen = time.time()
    return session


def active() -> list[Session]:
    with _lock:
        return [s for s in _sessions.values() if not s.expired]


def library_for(session: Session, library_id: int | None = None) -> dict[str, Any]:
    """Which library this person's downloads belong in.

    Derived from Navidrome rather than configured per user, so a new account
    needs no setup at all: they log in, and where their music goes is already
    known. Someone with a single library never has to choose.
    """
    libraries = session.identity.libraries
    if not libraries:
        raise ValueError(
            f"{session.identity.username} has no library in Navidrome, so "
            "there is nowhere to put a download.")
    if library_id is None:
        return libraries[0]
    for library in libraries:
        if str(library["id"]) == str(library_id):
            return library
    raise ValueError("That library does not belong to this account.")
