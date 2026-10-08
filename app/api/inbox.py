"""Uploading music into a person's inbox from the browser."""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from .. import auth, filer, inbox, navidrome, threads, uuidtags, workspace
from .deps import _prepare, current_session

# Every route here is for a signed-in person. Declared on the router as
# well as gated by the session middleware, so a route added here without
# its own Depends is still guarded, and the guard does not rest on a
# path prefix alone.
router = APIRouter(dependencies=[Depends(current_session)])


UPLOAD_PATH = "/api/inbox/upload"


# A form's own framing on top of the file: boundaries, part headers, the
# path and batch fields.
UPLOAD_OVERHEAD = 1024 * 1024


def _upload_refusal(headers) -> JSONResponse | None:
    """Refuse an upload too big to take, before any of it is read.

    The handler's own check came after Starlette had already spooled the
    whole multipart body to the container's /tmp, so one request of any
    size was stored first - on a Pi, on an SD card. Decided here from the
    declared length instead, which a browser always sends for a form.
    """
    length = headers.get("content-length")
    if length is None:
        return JSONResponse({"detail": "An upload has to say how big it is."},
                            status_code=411)
    try:
        size = int(length)
    except ValueError:
        return JSONResponse({"detail": "That upload's length is not a number."},
                            status_code=400)
    if size > MAX_UPLOAD_BYTES + UPLOAD_OVERHEAD:
        return JSONResponse({"detail": "That file is larger than this accepts."},
                            status_code=413)
    return None


# A FLAC track comfortably clears 40MB; refused well past that rather than
# guessed from a bitrate.
# Read and written a megabyte at a time, never whole.
UPLOAD_CHUNK = 1 << 20


MAX_UPLOAD_BYTES = 200 * 1024 * 1024


def _relpath_segments(relpath: str, filename: str | None) -> list[str]:
    """A browser-supplied path, broken into filesystem-safe components.

    `relpath` carries a dragged folder's structure when there is one (the
    browser has no equivalent for a plain file picker, so `filename` is the
    fallback). Sanitising component by component, after splitting, is what
    keeps a ``..`` segment from walking out of its upload folder - sanitizing
    the joined string would only turn its slashes into underscores and leave
    the dots untouched.

    A folder whose name starts with a dot loses the dot: the poller walks
    past hidden folders, so nothing uploaded under one was ever filed. The
    file's own name keeps it, for the caller to refuse.
    """
    raw = [part for part in (relpath or filename or "").replace("\\", "/").split("/")
           if part.strip()]
    if not raw:
        return []
    folders = [part.lstrip(".") for part in raw[:-1]]
    name = raw[-1]
    # sanitize() would turn the dot into an underscore and file the junk.
    hidden = name.startswith(".") and not name.startswith("..")
    last = f".{filer.sanitize(name[1:])}" if hidden else filer.sanitize(name)
    return [filer.sanitize(part) for part in folders if part.strip()] + [last]


@router.post("/api/inbox/upload")
async def upload_to_inbox(
    file: UploadFile = File(...),
    relpath: str = Form(""),
    batch: str | None = Form(None),
    library_id: int | None = Form(None),
    session: auth.Session = Depends(current_session),
) -> dict[str, str]:
    try:
        space = await asyncio.to_thread(
            workspace.for_session, session.identity, library_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    segments = _relpath_segments(relpath, file.filename)
    if not segments:
        raise HTTPException(status_code=400, detail="No filename given.")
    name = segments[-1]
    if name.startswith(".") and not name.startswith(".."):
        # macOS's ._ companions, mostly. Hidden from the poller, so it
        # would sit in the inbox for ever.
        raise HTTPException(
            status_code=400, detail=f"{name}: a hidden file, not filed.")
    suffix = Path(name).suffix.lower()
    if not (uuidtags.is_audio(Path(name)) or suffix in filer.COVER_SUFFIXES):
        raise HTTPException(
            status_code=400, detail=f"{name}: not something this can file.")

    # Checked before anything is read, and again while copying: the whole
    # upload used to be read into memory first - 200 MB a file, more since
    # the check came after, on a Raspberry Pi, several at once for a folder.
    too_big = HTTPException(
        status_code=413, detail=f"{name} is larger than this accepts.")
    if (getattr(file, "size", None) or 0) > MAX_UPLOAD_BYTES:
        await file.close()
        raise too_big

    await _prepare(space)

    # Server-generated on the first file of a drop and echoed back by every
    # later one in the same drop, rather than trusted from the browser - it
    # ends up as a path component, and a made-up value is cheap to sanitise
    # but has no reason to be trusted in the first place.
    batch = filer.sanitize(batch or uuid.uuid4().hex)
    target = inbox.upload_root(space, batch).joinpath(*segments)

    def write() -> int:
        """Copied in chunks to a hidden part file, then renamed into place,
        so the poller never sees half of it under its real name."""
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f".{target.name}.part")
        written = 0
        try:
            with open(partial, "wb") as out:
                while chunk := file.file.read(UPLOAD_CHUNK):
                    written += len(chunk)
                    if written > MAX_UPLOAD_BYTES:
                        break
                    out.write(chunk)
            if 0 < written <= MAX_UPLOAD_BYTES:
                # Not backdated here: a track backdated as it landed was
                # filed by the poller mid-upload, before the album's cover
                # (which often comes last) had arrived. `finish` releases
                # the whole drop at once.
                os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)
        return written

    try:
        written = await asyncio.to_thread(write)
    finally:
        await file.close()
    if not written:
        raise HTTPException(status_code=400, detail=f"{name} is empty.")
    if written > MAX_UPLOAD_BYTES:
        raise too_big
    return {"batch": batch}


@router.post("/api/inbox/upload/finish")
async def finish_upload(
    library_id: int | None = None,
    batch: str | None = None,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """File whatever has landed, instead of waiting for the next poll.

    Drains the whole inbox, not just the drop that just finished - anything
    else sitting there is this same person's, and there is no reason to make
    it wait. The regular poller would file it anyway within `POLL_SECONDS`;
    this only saves the browser watching a batch sit at "filing" for it.
    """
    try:
        space = await asyncio.to_thread(
            workspace.for_session, session.identity, library_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if batch:
        # Every file of this drop has arrived, so it can be filed now, all
        # together, rather than after the quiet period.
        await asyncio.to_thread(inbox.release, space, filer.sanitize(batch))
    result = await threads.run(inbox.drain, space)
    if result.changed:
        await asyncio.to_thread(navidrome.notify)
    return {
        "filed": [str(path.relative_to(space.library_path))
                  for path in result.filed],
        "failures": result.failures,
        "waiting": result.waiting,
    }
