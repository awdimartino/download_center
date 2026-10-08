"""Changing the Library: edits, matches, covers, combines, quarantine, ReplayGain."""

from __future__ import annotations

import asyncio
import functools
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import (
    auth,
    beets_runner,
    combine,
    covers,
    duplicates,
    filer,
    library,
    navidrome,
    operations,
    replaygain,
    store,
    threads,
    workspace,
)
from . import deps
from .deps import (
    _library_folder,
    _locked,
    _locked_request,
    _named,
    _not_arriving,
    _one_album,
    _reviewed,
    current_session,
)

# Every route here is for a signed-in person. Declared on the router as
# well as gated by the session middleware, so a route added here without
# its own Depends is still guarded, and the guard does not rest on a
# path prefix alone.
router = APIRouter(dependencies=[Depends(current_session)])


class LibraryQuarantine(BaseModel):
    library_id: int
    folder: str
    album: str = ""
    artist: str = ""


@router.post("/api/library/quarantine")
async def api_library_quarantine(
    body: LibraryQuarantine,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Set aside every track in one album row - the wrong record entirely,
    reached from the row itself.

    `library.tracks` is what validates the folder belongs to this account;
    every path quarantined below comes from that trusted read, never from
    the request body.
    """
    try:
        found = await asyncio.to_thread(
            library.tracks, session.identity, body.library_id, body.folder)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    folder = _library_folder(session.identity, body.library_id, body.folder)
    await asyncio.to_thread(_not_arriving, folder)
    copies = [duplicates.copy_from_track(t, body.library_id, body.album,
                                   t["artist"] or body.artist)
              for t in found["items"]]
    outcome = await _locked_request(
        [folder],
        functools.partial(duplicates.quarantine_many, copies, session.identity))

    if outcome.get("quarantined"):
        await asyncio.to_thread(navidrome.notify)
    return outcome


class TrackQuarantine(BaseModel):
    library_id: int
    folder: str
    track_id: str
    album: str = ""
    artist: str = ""


@router.post("/api/library/track/quarantine")
async def api_track_quarantine(
    body: TrackQuarantine,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Set aside one track by hand - the wrong file entirely, not a worse
    copy of a right one.

    Scoped to the album it claims to be in, the same way
    `/api/library/quarantine` is scoped to a folder: `library.tracks`
    validates ownership, and the track has to be one it actually reports
    there, so a path can only ever come from that trusted read.
    """
    try:
        found = await asyncio.to_thread(
            library.tracks, session.identity, body.library_id, body.folder)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    track = next((t for t in found["items"] if t["id"] == body.track_id), None)
    if track is None:
        raise HTTPException(status_code=404,
                            detail="That track is not in this album.")

    folder = _library_folder(session.identity, body.library_id, body.folder)
    await asyncio.to_thread(_not_arriving, folder)
    copy = duplicates.copy_from_track(track, body.library_id, body.album,
                                track["artist"] or body.artist)
    try:
        moved = await _locked_request(
            [folder],
            functools.partial(duplicates.quarantine_one, copy, session.identity))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await asyncio.to_thread(navidrome.notify)
    return {"quarantined": [moved], "failed": []}


class AlbumEdit(BaseModel):
    library_id: int
    folder: str
    album_artist: str
    album: str


class TrackEdit(BaseModel):
    library_id: int
    path: str
    title: str | None = None
    artist: str | None = None
    track_no: int | None = None
    disc_no: int | None = None
    album_artist: str | None = None
    album: str | None = None


class ReviewMark(BaseModel):
    library_id: int
    folder: str
    reviewed: bool = True


@router.post("/api/library/reviewed")
async def library_reviewed(
    body: ReviewMark,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Say an album has been dealt with, or put it back on the list.

    For the albums nothing else will ever mark: tagged correctly by hand,
    and never going to be in MusicBrainz.
    """
    try:
        ids = await asyncio.to_thread(
            library.album_ids, session.identity, body.library_id, body.folder)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if body.reviewed:
        store.mark_reviewed(body.library_id, ids, "marked",
                            session.identity.username)
    else:
        store.unmark_reviewed(body.library_id, ids)
    return {"reviewed": body.reviewed}


@router.post("/api/library/album/edit")
async def library_album_edit(
    body: AlbumEdit,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Rename a whole album, and move its files to match.

    Album-level because one album is one UUID: the artist and the title are
    properties of the folder, and editing them on a single track is how a
    record becomes two. The album keeps its identity through the change, so
    album-level stars and play counts survive it - unless the new name is
    already taken, in which case these files join the record that is there.
    """
    _named(body.album_artist, body.album)

    def check() -> tuple[workspace.Workspace, Path]:
        try:
            space = workspace.for_session(session.identity, body.library_id)
            folder = library.album_dir(
                session.identity, body.library_id, body.folder)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _not_arriving(folder)
        return space, folder

    space, folder = await asyncio.to_thread(check)

    # Where it is going as well as where it is: a rename into an album that
    # something else is changing would land in the middle of that change.
    going = filer.album_folder(space, body.album_artist, body.album)

    def run() -> dict[str, Any]:
        ids = deps._before_edit(session.identity, body.library_id, body.folder)
        filed = filer.retag_album(space, folder,
                                  albumartist=body.album_artist,
                                  album=body.album)
        _reviewed(session.identity, body.library_id, ids, "edited")
        navidrome.notify()
        return {"ran": True, "moved": len(filed),
                "folder": str(filed[0].path.parent.relative_to(
                    space.library_path)) if filed else body.folder,
                "album_uuid": filed[0].album_uuid if filed else None,
                "failed": filer.unidentified(filed)}

    return await _locked_request(list({folder, going}), run)


@router.post("/api/library/track/edit")
async def library_track_edit(
    body: TrackEdit,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Change one track, and move it if its tags now say it belongs elsewhere.

    Title and numbers rename it inside its own folder. Artist or album take
    it *out* of its album and into another - which is what rescues a track
    filed under the wrong record, and what splits one if it is done by
    mistake. The browser confirms that before asking.
    """
    _named(body.album_artist, body.album)
    if all(value is None for value in
           (body.title, body.artist, body.track_no, body.disc_no,
            body.album_artist, body.album)):
        raise HTTPException(status_code=400, detail="Nothing to change.")
    def check() -> tuple[workspace.Workspace, Path]:
        try:
            space = workspace.for_session(session.identity, body.library_id)
            path = library.track_path(session.identity, body.library_id, body.path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _not_arriving(path)
        return space, path

    space, path = await asyncio.to_thread(check)

    def run() -> dict[str, Any]:
        ids = deps._before_edit(session.identity, body.library_id,
                           library.folder_of(body.path))
        filed = filer.retag_track(
            space, path, albumartist=body.album_artist, album=body.album,
            artist=body.artist, title=body.title,
            track_no=body.track_no, disc_no=body.disc_no)
        # The album it was in: that is the one somebody was going through.
        _reviewed(session.identity, body.library_id, ids, "edited")
        navidrome.notify()
        return {"ran": True,
                "path": str(filed.path.relative_to(space.library_path)),
                # Left this album's folder, so the panel stops listing it.
                "moved": filed.path.parent != path.parent,
                "album_uuid": filed.album_uuid,
                "identified": filed.identified}

    return await _locked_request([path.parent], run)


@router.post("/api/library/rescan")
async def library_rescan(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Ask Navidrome to look again, now.

    The list is read from Navidrome's database, which refreshes on scan, so
    it can be a few minutes behind what is on disk. That is fine for a page
    somebody opens deliberately and not fine when they have just changed
    something and want to see it.
    """
    if not navidrome.service_configured():
        raise HTTPException(
            status_code=400,
            detail="Navidrome's address and service credentials are not set, "
                   "so a scan cannot be requested from here.")
    # Asked separately from whether it worked. `notify` reports both as
    # False, and telling somebody to fix credentials that are already right
    # sends them to the one place the problem is not.
    if not await asyncio.to_thread(navidrome.notify):
        raise HTTPException(
            status_code=502,
            detail="Navidrome did not answer, so it has not been asked to "
                   "scan. The list is still correct as of its last one.")
    return {"scanning": True}


class AlbumTarget(BaseModel):
    library_id: int
    folder: str


@router.post("/api/library/match")
async def library_match(
    body: AlbumTarget,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """What this album would match against, asked on demand.

    Matching is manual now, one item at a time, from this page. Nothing here
    writes anything: the caller gets the candidate list that `quiet_fallback:
    skip` used to throw away, and a person picks from it. Applying a choice
    belongs to the tagging page, which is designed separately.

    An operation rather than a plain request: a candidate lookup is several
    MusicBrainz round trips and took up to seventy seconds against the real
    backlog, which is far longer than a request should be held open. The
    answer arrives over the websocket.
    """
    def check() -> tuple[workspace.Workspace, Path]:
        try:
            space = workspace.for_session(session.identity, body.library_id)
            path = library.album_dir(session.identity, body.library_id, body.folder)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _one_album(path)
        return space, path

    space, path = await asyncio.to_thread(check)

    target = {"library_id": body.library_id, "folder": body.folder}

    def run() -> dict[str, Any]:
        result = beets_runner.candidates(space, path)
        # Named in the answer, because the answer arrives over the websocket
        # long after the request: the page drops a list that is not about
        # the album it is showing, and applying checks the same record.
        result.update(target)
        _offer((session.identity.username, body.library_id, str(path)),
               {c["id"] for c in result.get("candidates", []) if c.get("id")})
        return result

    operation, started = operations.start(
        "candidates", session.identity.username, run, target=target)
    return {"started": started, "operation": operation.as_dict()}


# Which releases each album was offered, by (user, library, album folder).
# Applying refuses anything else. A lookup started while another was in
# flight used to be handed that one's answer, drawn under the wrong album,
# and *Use this* then retagged one album as another's release - fusing them.
# In memory: after a restart, finding matches again is the cost.
_offered: dict[tuple[str, int, str], set[str]] = {}


_offered_at: dict[tuple[str, int, str], float] = {}


# How long a list of matches can be applied from. Kept for ever, every Find
# matches on every album added a set that was never removed.
OFFER_SECONDS = 24 * 60 * 60


def _offer(key: tuple[str, int, str], ids: set[str]) -> None:
    now = time.time()
    for old, when in list(_offered_at.items()):
        if now - when > OFFER_SECONDS:
            _offered.pop(old, None)
            del _offered_at[old]
    _offered[key] = ids
    _offered_at[key] = now


class AlbumChoice(BaseModel):
    library_id: int
    folder: str
    release_id: str


@router.post("/api/library/match/apply")
async def library_match_apply(
    body: AlbumChoice,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Tag an album as the release somebody picked from the candidate list.

    The one thing allowed to move a file after it is written. It is
    deliberate, rare and watched: a person looked at a list and pointed at a
    row. The album UUID is re-pointed rather than reissued, so the record
    keeps its Navidrome identity and album-level stars and play counts
    survive the retag.
    """
    def check() -> tuple[workspace.Workspace, Path]:
        try:
            space = workspace.for_session(session.identity, body.library_id)
            path = library.album_dir(session.identity, body.library_id, body.folder)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        offered = _offered.get(
            (session.identity.username, body.library_id, str(path)), set())
        if body.release_id not in offered:
            raise HTTPException(
                status_code=409,
                detail=f"That release was not offered for {path.name}; "
                       "find matches for it again.")
        _one_album(path)
        # A download of this album still filing tracks into it: retagging
        # mid-flight re-points the registry, and the tracks that land
        # afterwards still carry the old tags, miss the key that has just
        # moved, and found a second album beside the first.
        _not_arriving(path)
        return space, path

    space, path = await asyncio.to_thread(check)

    def run() -> dict[str, Any]:
        ids = deps._before_edit(session.identity, body.library_id, body.folder)
        result = beets_runner.apply_release(space, path, body.release_id)
        if not result.get("imported"):
            return result
        # Last, so an album whose retag did not go through stays in review.
        _reviewed(session.identity, body.library_id, ids, "matched")
        navidrome.notify()
        return result

    operation, started = operations.start(
        "import", session.identity.username,
        functools.partial(_locked, [path], run))
    return {"started": started, "operation": operation.as_dict()}


class CoverChoice(BaseModel):
    library_id: int
    folder: str
    # One of the URLs `library_cover_candidates` offered, or none at all to
    # square the cover the album already has.
    url: str | None = None


def _cover_target(session: auth.Session, library_id: int,
                  folder: str) -> tuple[Path, list[Path]]:
    try:
        path = library.album_dir(session.identity, library_id, folder)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    tracks = filer.audio_in(path)
    if not tracks:
        raise HTTPException(status_code=404, detail="That album has no tracks on disk.")
    return path, tracks


@router.post("/api/library/cover/candidates")
async def library_cover_candidates(
    body: AlbumTarget,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Covers this album could have, to choose between - and nothing else.

    "Find matches" brings art too, but it also rewrites every tag from
    MusicBrainz and can move the files. This is for when the picture is the
    only thing wrong, which is every YouTube download that arrived with bars.
    """
    path, tracks = await asyncio.to_thread(
        _cover_target, session, body.library_id, body.folder)

    def run() -> list[dict[str, Any]]:
        meta = filer.read_meta(tracks[0])
        album = meta.album if meta.names_album else meta.title
        return covers.candidates(path, tracks, meta.albumartist, album)

    return {"candidates": await threads.run(run)}


@router.post("/api/library/cover/apply")
async def library_cover_apply(
    body: CoverChoice,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Put the chosen cover on every track of the album, and nothing else."""
    if body.url is not None and not covers.choosable(body.url):
        raise HTTPException(status_code=400,
                            detail="That cover is not one this offered.")
    def check() -> tuple[Path, list[Path]]:
        path, tracks = _cover_target(session, body.library_id, body.folder)
        _not_arriving(path)
        _one_album(path)
        return path, tracks

    path, tracks = await asyncio.to_thread(check)

    def run() -> dict[str, Any]:
        try:
            result = covers.apply_choice(path, tracks, body.url)
        except covers.NoCover as exc:
            raise HTTPException(status_code=502 if exc.fetched else 404,
                                detail=str(exc)) from exc
        if not result.get("already_square"):
            navidrome.notify()
        return result

    return await _locked_request([path], run)


class CombineRequest(BaseModel):
    library_id: int
    albumartist: str
    album: str
    # Whole album folders, and single tracks picked out of other albums -
    # both as the library listing names them.
    albums: list[str] = []
    tracks: list[str] = []
    # The selected album whose identity survives. None lets the first carry
    # it, or an album already called `albumartist`/`album` win if there is one.
    keep: str | None = None
    # Every file, in the order to number them. Empty leaves numbers alone.
    order: list[str] = []
    # One of the selected album folders to take the cover from, or a URL
    # `combine/guess` offered.
    cover_folder: str | None = None
    cover_url: str | None = None


@router.post("/api/library/combine")
async def library_combine(
    body: CombineRequest,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Make several albums and loose tracks into one album.

    An operation, because a combine of three albums is dozens of files each
    retagged and moved, which on the Pi takes longer than a request should
    be held open. Progress and the outcome arrive over the websocket.
    """
    _named(body.albumartist, body.album)
    identity = session.identity

    def resolve() -> tuple[workspace.Workspace, dict[str, Path], dict[str, Path]]:
        try:
            return (workspace.for_session(identity, body.library_id),
                    {name: library.album_dir(identity, body.library_id, name)
                     for name in dict.fromkeys(body.albums)},
                    {name: library.track_path(identity, body.library_id, name)
                     for name in dict.fromkeys(body.tracks + body.order)})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    space, folders, files = await asyncio.to_thread(resolve)

    if body.keep is not None and body.keep not in folders:
        raise HTTPException(status_code=400,
                            detail="The album to keep has to be one of those selected.")
    if body.cover_folder is not None and body.cover_folder not in folders:
        raise HTTPException(status_code=400,
                            detail="The cover has to come from one of those selected.")
    if body.cover_url is not None and not covers.choosable(body.cover_url):
        raise HTTPException(status_code=400,
                            detail="That cover is not one this offered.")

    def check() -> None:
        # Distinct files: a track picked out of a folder that is itself
        # selected, or named twice, is still one track.
        chosen = {one.resolve() for path in folders.values()
                  for one in filer.audio_in(path)}
        chosen.update(files[name].resolve() for name in body.tracks)
        if len(chosen) < 2:
            raise HTTPException(status_code=400,
                                detail="Choose at least two tracks to combine.")
        for path in [*folders.values(), *(files[name] for name in body.tracks)]:
            _not_arriving(path)
        for path in folders.values():
            _one_album(path)

    await asyncio.to_thread(check)

    def run() -> dict[str, Any]:
        survivor = body.keep or next(iter(folders), None)
        ids = (deps._before_edit(identity, body.library_id, survivor)
               if survivor else set())
        # Read before anything moves: the folder it lives in may not
        # survive the combine.
        cover = None
        if body.cover_url:
            cover = covers.fetch(body.cover_url, covers.CHOOSABLE_HOSTS)
        elif body.cover_folder:
            path = folders[body.cover_folder]
            cover = covers.current(path, filer.audio_in(path))
        result = combine.combine(
            space, albumartist=body.albumartist.strip(),
            album=body.album.strip(),
            albums=list(folders.values()),
            tracks=[files[name] for name in body.tracks],
            keep=folders.get(body.keep) if body.keep else None,
            order=[files[name] for name in body.order],
            cover=cover,
            report=functools.partial(operations.report, "combine",
                                     identity.username))
        _reviewed(identity, body.library_id, ids, "edited")
        navidrome.notify()
        return result

    operation, started = operations.start(
        "combine", identity.username,
        functools.partial(_locked, [*folders.values(),
                                    *(files[name].parent for name in body.tracks),
                                    # And the album they are going into.
                                    filer.album_folder(space, body.albumartist.strip(),
                                                       body.album.strip())],
                          run))
    return {"started": started, "operation": operation.as_dict()}


class CombineGuess(BaseModel):
    artist: str
    titles: list[str]


@router.post("/api/library/combine/guess")
async def library_combine_guess(
    body: CombineGuess,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Which album these songs are probably from, to name the combine.

    A suggestion for the form, never applied by itself: an empty answer is
    normal, and the form just leaves the name for a person to type.
    """
    guess = await asyncio.to_thread(combine.guess_album, body.artist,
                                    body.titles)
    return {"guess": guess}


class GainTarget(BaseModel):
    # Both absent: every album in this person's libraries with a track that
    # has no ReplayGain.
    library_id: int | None = None
    folder: str | None = None


@router.post("/api/library/replaygain")
async def library_replaygain(
    body: GainTarget,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Measure ReplayGain for one album, or for every album missing some.

    An operation, reporting progress album by album: "all missing" was
    about 3,400 tracks when this was written, which is a long time on a Pi
    and indistinguishable from a hang without a count going up.
    """
    if not replaygain.available():
        raise HTTPException(
            status_code=503,
            detail="rsgain is not installed in this container, so nothing "
                   "can be measured.")
    identity = session.identity
    try:
        if body.folder is not None:
            if body.library_id is None:
                raise ValueError("Say which library the album is in.")
            await asyncio.to_thread(functools.partial(
                library.album_dir, identity, body.library_id, body.folder,
                any_depth=True))
            targets = [(body.library_id, body.folder)]
        else:
            targets = await asyncio.to_thread(library.without_gain, identity)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not targets:
        raise HTTPException(status_code=400,
                            detail="Every album already has ReplayGain.")

    def run() -> dict[str, Any]:
        return replaygain.measure_all(identity, targets)

    operation, started = operations.start(
        replaygain.NAME, identity.username, run)
    return {"started": started, "operation": operation.as_dict()}


@router.post("/api/library/replaygain/stop")
async def library_replaygain_stop(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Finish the album in hand, then stop. Only your own run: there is no
    way to name anybody else's."""
    return {"operation": operations.stop(
        replaygain.NAME, session.identity.username).as_dict()}
