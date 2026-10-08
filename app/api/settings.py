"""The installation's settings."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import auth, config, spotify
from ..config import settings
from .deps import admin_session, current_session

log = logging.getLogger("navidrome_companion")
# Every route here is for a signed-in person. Declared on the router as
# well as gated by the session middleware, so a route added here without
# its own Depends is still guarded, and the guard does not rest on a
# path prefix alone.
router = APIRouter(dependencies=[Depends(current_session)])


class SettingsUpdate(BaseModel):
    spotify_client_id: str | None = None
    spotify_client_secret: str | None = None
    concurrency: int | None = None
    audio_bitrate: str | None = None
    max_attempts: int | None = None
    rate_limit_sleep: float | None = None
    navidrome_url: str | None = None
    navidrome_user: str | None = None
    navidrome_password: str | None = None
    acoustid_key: str | None = None
    # Listed in config.EDITABLE and returned by GET, so it has to be settable
    # or the two disagree about what "editable" means.
    beets_enabled: bool | None = None


# Values the browser must never be sent back. Reported as a boolean instead,
# so a form can show whether one is set without ever holding it.
#
# The AcoustID key is here with the passwords rather than with the Spotify
# client id. It identifies an application to a service that rate-limits and
# can ban by key, so handing it to every admin's browser session is a way to
# lose it - and unlike the client id, nothing in the page needs to read it
# back.
SECRETS = ("spotify_client_secret", "navidrome_password", "acoustid_key")


@router.get("/api/settings")
async def get_settings(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """These settings belong to the installation, so only an admin sees them.

    Secrets were already masked, but the rest was not: any signed-in account
    got the Navidrome service URL and username and the Spotify client id.
    The form is disabled for them anyway, so there was nothing to show and
    something to leak.
    """
    if not session.identity.is_admin:
        return {"editable": False}

    values = {key: getattr(settings, key) for key in config.EDITABLE}
    values["editable"] = True
    # Set by the container's environment: shown, but not editable here.
    values["locked"] = {key: config.env_var(key) for key in config.EDITABLE
                        if key in config.FROM_ENV}
    for key in SECRETS:
        values[key] = ""
        values[f"{key}_set"] = bool(getattr(settings, key))
    return values


@router.put("/api/settings")
async def put_settings(
    update: SettingsUpdate,
    session: auth.Session = Depends(admin_session),
) -> dict[str, Any]:
    changes = {k: v for k, v in update.model_dump().items() if v is not None}
    # A blank secret means "leave it alone", since the form never receives it.
    for key in SECRETS:
        if not changes.get(key):
            changes.pop(key, None)
    if not changes:
        return await get_settings(session)
    locked = sorted(key for key in changes if key in config.FROM_ENV)
    if locked:
        raise HTTPException(
            status_code=400,
            detail=f"{locked[0]} is set by {config.env_var(locked[0])} in the "
                   "container's environment; change it there.")

    try:
        # Validate against the model before touching the live settings.
        validated = settings.__class__(**{**settings.model_dump(), **changes})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc).split(chr(10))[0]) from exc
    # What is stored is what the model made of it, not what was typed: "320k"
    # validates because the model drops the k, and the raw string then failed
    # every download until a restart read it back through the model.
    changes = {key: getattr(validated, key) for key in changes}

    await asyncio.to_thread(config.save, changes)
    if "spotify_client_id" in changes or "spotify_client_secret" in changes:
        spotify.reset_client()
    log.info("settings updated: %s", ", ".join(sorted(changes)))
    return await get_settings(session)


@router.get("/api/status")
async def status(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    return {
        "spotify_configured": settings.spotify_configured,
        "output_dir": str(settings.output_dir),
        "concurrency": settings.concurrency,
    }
