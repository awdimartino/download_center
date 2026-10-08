"""Signing in and out."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from .. import auth, events, navidrome
from ..config import settings
from .deps import _send_cookie

log = logging.getLogger("navidrome_companion")
router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


# Failed sign-ins allowed per address in a window. Navidrome has its own
# limits, but this endpoint is open to anyone who can reach the port, and
# each attempt is a request to Navidrome on this person's behalf.
SIGN_IN_FAILURES = 10


SIGN_IN_WINDOW = 10 * 60


_sign_in_failures: dict[str, list[float]] = {}


def _recent_failures(address: str) -> list[float]:
    """This address's failed sign-ins inside the window. Every address's
    old ones are dropped on the way: only the address asking was ever
    tidied, so each address that failed once stayed for good."""
    cutoff = time.time() - SIGN_IN_WINDOW
    for who in list(_sign_in_failures):
        kept = [t for t in _sign_in_failures[who] if t > cutoff]
        if kept:
            _sign_in_failures[who] = kept
        else:
            del _sign_in_failures[who]
    return _sign_in_failures.get(address, [])


@router.post("/api/auth/login")
async def sign_in(request: Request, body: LoginRequest,
                  response: Response) -> dict[str, Any]:
    address = request.client.host if request.client else "?"
    if len(_recent_failures(address)) >= SIGN_IN_FAILURES:
        raise HTTPException(
            status_code=429,
            detail="Too many failed sign-ins. Wait a few minutes and try again.")
    try:
        session = await asyncio.to_thread(
            auth.sign_in, body.username, body.password)
    except navidrome.LoginFailed as exc:
        _sign_in_failures.setdefault(address, []).append(time.time())
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except navidrome.NotConfigured as exc:
        log.warning("sign-in refused: Navidrome is not configured: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Navidrome is not configured. An administrator needs to "
                   "set its address.") from exc
    except Exception as exc:
        # The reason stays in the log. It names internal hosts and ports,
        # and this answer goes to anyone, signed in or not.
        log.warning("sign-in could not reach Navidrome: %s", exc)
        raise HTTPException(
            status_code=502, detail="Could not reach Navidrome.") from exc
    _sign_in_failures.pop(address, None)

    _send_cookie(response, request, session)
    return session.as_dict()


@router.post("/api/auth/logout")
async def sign_out(request: Request, response: Response) -> dict[str, bool]:
    session_id = request.cookies.get(auth.COOKIE) or ""
    auth.sign_out(session_id)
    if session_id:
        await events.broker.close_session(session_id)
    response.delete_cookie(auth.COOKIE)
    return {"signed_out": True}


@router.get("/api/auth/me")
async def whoami(request: Request) -> dict[str, Any]:
    session = auth.get(request.cookies.get(auth.COOKIE))
    if session is None:
        return {"signed_in": False,
                "navidrome_configured": bool(settings.navidrome_url)}
    return {"signed_in": True, **session.as_dict()}
