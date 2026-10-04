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

log = logging.getLogger("navidrome_companion.auth")

COOKIE = "dc_session"
# Long enough not to interrupt an afternoon, short enough that a forgotten
# browser on a shared machine does not stay signed in indefinitely.
LIFETIME_SECONDS = 14 * 24 * 60 * 60


# How often a session with no libraries asks Navidrome again. Frequent enough
# that a database blip at sign-in heals by itself, rare enough that an account
# genuinely assigned none does not open the database on every request.
RECHECK_SECONDS = 60

# How often every session re-reads its admin flag and libraries. Both used to
# be fixed at sign-in, and the lifetime slides while used, so a demoted
# admin stayed admin and a revoked library stayed editable for as long as
# somebody kept the tab open.
PRIVILEGES_SECONDS = 300

# However active, a session ends this long after sign-in.
MAX_AGE_SECONDS = 30 * 24 * 60 * 60


@dataclass
class Session:
    id: str
    identity: navidrome.Identity
    created_at: float
    last_seen: float
    libraries_checked_at: float = 0.0

    @property
    def expired(self) -> bool:
        now = time.time()
        return (now - self.last_seen > LIFETIME_SECONDS
                or now - self.created_at > MAX_AGE_SECONDS)

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
    session = Session(secrets.token_urlsafe(32), identity, now, now, now)
    with _lock:
        _sessions[session.id] = session
    log.info("%s signed in (%d librar%s)", identity.username,
             len(identity.libraries),
             "y" if len(identity.libraries) == 1 else "ies")
    return session


def sign_out(session_id: str) -> None:
    with _lock:
        _sessions.pop(session_id, None)


# When expired sessions were last swept out, so it happens on a timer rather
# than on every request.
_last_sweep = 0.0
SWEEP_SECONDS = 300


def _sweep_locked() -> None:
    """Drop expired sessions. Caller holds the lock.

    A session was only ever removed when somebody presented that exact
    cookie, so one that simply stopped being used stayed in the dict for the
    life of the process - holding a live Navidrome bearer token for a
    fortnight past its usefulness.
    """
    global _last_sweep
    now = time.time()
    if now - _last_sweep < SWEEP_SECONDS:
        return
    _last_sweep = now
    stale = [sid for sid, s in _sessions.items() if s.expired]
    for sid in stale:
        del _sessions[sid]
    if stale:
        log.info("dropped %d expired session(s)", len(stale))


def get(session_id: str | None) -> Session | None:
    if not session_id:
        return None
    with _lock:
        _sweep_locked()
        session = _sessions.get(session_id)
        if session is None:
            return None
        if session.expired:
            del _sessions[session.id]
            return None
        session.last_seen = time.time()

    # Admin status and libraries are re-read from Navidrome every few
    # minutes - every minute while there are no libraries, so a database
    # blip at sign-in heals quickly. Rate-limited: this runs on the request
    # path, on the event loop.
    every = (RECHECK_SECONDS if not session.identity.libraries
             else PRIVILEGES_SECONDS)
    if time.time() - session.libraries_checked_at > every:
        session.libraries_checked_at = time.time()
        try:
            current = navidrome.account(session.identity)
        except Exception as exc:
            # Unreadable is not "revoked": keep what the session had.
            log.debug("could not re-read %s's account: %s",
                      session.identity.username, exc)
        else:
            if current is None:
                log.info("%s no longer exists in Navidrome; signing out",
                         session.identity.username)
                sign_out(session.id)
                return None
            session.identity.is_admin, session.identity.libraries = current
    return session


def active() -> list[Session]:
    with _lock:
        return [s for s in _sessions.values() if not s.expired]


