"""Several albums and loose tracks, made into one album.

Real MP3s on a real filesystem, through the filer's own retag. What matters
is what the old one-rename-at-a-time route got wrong: which album keeps its
identity, that nothing is left behind, and that the numbering and the cover
end up describing one record rather than five.
"""

from __future__ import annotations

import io

import pytest
from mutagen.easyid3 import EasyID3
from PIL import Image

from app import combine, covers, filer, registry, uuidtags
from test_filer import album_on_disk, space, track, tmp_of  # noqa: F401


def _single(space, artist, title, tracknumber=None):
    """A YouTube download: an album of one, named for its song."""
    tags = {"albumartist": artist, "artist": artist, "album": title,
            "title": title}
    if tracknumber:
        tags["tracknumber"] = tracknumber
    return filer.file_track(space, track(tmp_of(space), name=f"{title}.mp3",
                                         **tags))


def _tags(path):
    audio = EasyID3(path)
    return {key: audio.get(key, [""])[0]
            for key in ("albumartist", "album", "title", "tracknumber")}


def test_singles_become_one_album_under_a_new_name(space):
    singles = [_single(space, "Phoebe Bridgers", t)
               for t in ("Motion Sickness", "Scott Street", "Funeral")]

    result = combine.combine(
        space, albumartist="Phoebe Bridgers", album="Stranger in the Alps",
        albums=[s.path.parent for s in singles], tracks=[])

    target = space.library_path / "Phoebe Bridgers" / "Stranger in the Alps"
    assert result["moved"] == 3 and result["failed"] == []
    assert sorted(p.name for p in filer.audio_in(target)) == [
        "Funeral.mp3", "Motion Sickness.mp3", "Scott Street.mp3"]
    for single in singles:
        assert not single.path.parent.exists(), "the single's folder is pruned"


def test_every_track_ends_up_on_one_album_uuid(space):
    singles = [_single(space, "Phoebe Bridgers", t)
               for t in ("Motion Sickness", "Scott Street")]

    combine.combine(space, albumartist="Phoebe Bridgers",
                    album="Stranger in the Alps",
                    albums=[s.path.parent for s in singles], tracks=[])

    target = space.library_path / "Phoebe Bridgers" / "Stranger in the Alps"
    uuids = {uuidtags.read(p)[1] for p in filer.audio_in(target)}
    assert len(uuids) == 1


def test_the_kept_album_keeps_its_identity(space):
    """Album-level stars and play counts hang off the UUID, so the album the
    person chose to keep must still carry its own afterwards."""
    album = album_on_disk(space, "Phoebe Bridgers", "Punisher",
                          ["Garden Song", "Kyoto"])
    stray = _single(space, "Phoebe Bridgers", "Halloween")

    combine.combine(space, albumartist="Phoebe Bridgers", album="Punisher",
                    albums=[stray.path.parent, album[0].path.parent],
                    tracks=[], keep=album[0].path.parent)

    target = album[0].path.parent
    assert {uuidtags.read(p)[1] for p in filer.audio_in(target)} \
        == {album[0].album_uuid}
    assert len(filer.audio_in(target)) == 3


def test_keeping_an_album_under_a_new_name_moves_its_identity_with_it(space):
    album = album_on_disk(space, "Phoebe Bridgers", "Strangr in the Alps",
                          ["Smoke Signals"])
    stray = _single(space, "Phoebe Bridgers", "Funeral")

    combine.combine(space, albumartist="Phoebe Bridgers",
                    album="Stranger in the Alps",
                    albums=[album[0].path.parent, stray.path.parent],
                    tracks=[], keep=album[0].path.parent)

    assert registry.known(space.library_id, registry.album_key(
        "Phoebe Bridgers", "Stranger in the Alps")) == album[0].album_uuid


def test_a_loose_track_leaves_its_album_and_the_rest_stays(space):
    source = album_on_disk(space, "Various", "Mixtape", ["Keep Me", "Take Me"])
    target = album_on_disk(space, "Alvvays", "Blue Rev", ["Pharmacist"])

    combine.combine(space, albumartist="Alvvays", album="Blue Rev",
                    albums=[target[0].path.parent], tracks=[source[1].path],
                    keep=target[0].path.parent)

    assert source[0].path.is_file(), "the track nobody picked stayed"
    assert len(filer.audio_in(target[0].path.parent)) == 2


def test_a_track_inside_a_selected_album_is_not_moved_twice(space):
    album = album_on_disk(space, "A", "Record", ["One", "Two"])
    other = _single(space, "A", "Three")

    result = combine.combine(space, albumartist="A", album="Record",
                             albums=[album[0].path.parent, other.path.parent],
                             tracks=[album[0].path], keep=album[0].path.parent)

    assert result["moved"] == 3


def test_the_order_given_is_the_numbering_written(space):
    singles = [_single(space, "Phoebe Bridgers", t, tracknumber="1")
               for t in ("Scott Street", "Motion Sickness", "Funeral")]
    order = [singles[1].path, singles[0].path, singles[2].path]

    combine.combine(space, albumartist="Phoebe Bridgers",
                    album="Stranger in the Alps",
                    albums=[s.path.parent for s in singles], tracks=[],
                    order=order)

    target = space.library_path / "Phoebe Bridgers" / "Stranger in the Alps"
    named = {_tags(p)["title"]: (_tags(p)["tracknumber"], p.name)
             for p in filer.audio_in(target)}
    assert named == {
        "Motion Sickness": ("1/3", "01 - Motion Sickness.mp3"),
        "Scott Street": ("2/3", "02 - Scott Street.mp3"),
        "Funeral": ("3/3", "03 - Funeral.mp3"),
    }


def test_without_an_order_the_numbers_are_left_alone(space):
    album = album_on_disk(space, "A", "Record", ["One", "Two"])
    other = _single(space, "A", "Seven", tracknumber="7")

    combine.combine(space, albumartist="A", album="Record",
                    albums=[album[0].path.parent, other.path.parent],
                    tracks=[], keep=album[0].path.parent)

    numbers = sorted(_tags(p)["tracknumber"]
                     for p in filer.audio_in(album[0].path.parent))
    assert numbers == ["1", "2", "7"]


def test_the_chosen_cover_goes_on_every_track(space):
    singles = [_single(space, "A", t) for t in ("One", "Two")]
    frame = Image.new("RGB", (1280, 720), (0, 0, 0))
    out = io.BytesIO()
    frame.save(out, "JPEG")

    result = combine.combine(space, albumartist="A", album="Record",
                             albums=[s.path.parent for s in singles],
                             tracks=[], cover=out.getvalue())

    target = space.library_path / "A" / "Record"
    assert result["cover"] is True
    for path in filer.audio_in(target):
        assert Image.open(io.BytesIO(covers.embedded(path))).size == (720, 720)


def test_joining_an_album_that_was_not_selected_merges_into_it(space):
    """Typing the name of an album already in the library is how a person
    adds to it without having to find and tick it first."""
    incumbent = album_on_disk(space, "Alvvays", "Blue Rev", ["Pharmacist"])
    singles = [_single(space, "Alvvays", t) for t in ("Belinda Says", "Pressed")]

    combine.combine(space, albumartist="Alvvays", album="Blue Rev",
                    albums=[s.path.parent for s in singles], tracks=[])

    target = incumbent[0].path.parent
    assert len(filer.audio_in(target)) == 3
    assert {uuidtags.read(p)[1] for p in filer.audio_in(target)} \
        == {incumbent[0].album_uuid}


def test_progress_is_reported_per_file(space):
    singles = [_single(space, "A", t) for t in ("One", "Two")]
    seen = []

    combine.combine(space, albumartist="A", album="Record",
                    albums=[s.path.parent for s in singles], tracks=[],
                    report=lambda **p: seen.append(p))

    assert [(p["done"], p["total"]) for p in seen] == [(1, 2), (2, 2)]


# --- naming it ---------------------------------------------------------------

def _hit(album, kind="album"):
    return {"album": {"name": album, "album_type": kind,
                      "artists": [{"name": "Phoebe Bridgers"}],
                      "release_date": "2017-09-22",
                      "images": [{"url": "https://i.scdn.co/image/x"}]}}


def test_the_album_most_songs_are_on_is_the_guess(monkeypatch):
    from app import spotify

    answers = {
        "Motion Sickness": [_hit("Stranger in the Alps"), _hit("Motion Sickness", "single")],
        "Scott Street": [_hit("Stranger in the Alps (Deluxe)"), _hit("Stranger in the Alps")],
        "Funeral": [_hit("Stranger in the Alps")],
    }
    monkeypatch.setattr(spotify, "search", lambda q, kind, limit: next(
        v for k, v in answers.items() if k in q))

    guess = combine.guess_album("Phoebe Bridgers", list(answers))

    assert guess["album"] == "Stranger in the Alps"
    assert guess["votes"] == 3
    assert guess["year"] == "2017"


def test_one_song_is_not_enough_to_name_several(monkeypatch):
    from app import spotify

    monkeypatch.setattr(spotify, "search", lambda q, kind, limit:
                        [_hit("Some Album")] if "One" in q else [])

    assert combine.guess_album("A", ["One", "Two"]) is None


def test_no_spotify_means_no_guess(monkeypatch):
    from app import spotify

    def broken(*a, **k):
        raise spotify.ResolveError("not configured")

    monkeypatch.setattr(spotify, "search", broken)
    assert combine.guess_album("A", ["One", "Two"]) is None


@pytest.mark.parametrize("kind", ["single", "ep"])
def test_singles_and_eps_do_not_vote(monkeypatch, kind):
    from app import spotify

    monkeypatch.setattr(spotify, "search",
                        lambda q, k, limit: [_hit("Its Own Single", kind)])
    assert combine.guess_album("A", ["One"]) is None


# --- CODE_REVIEW M12 ---------------------------------------------------------

def test_an_album_already_called_that_joins_the_rest_in_one_folder(space):
    """It was skipped as already named, so it stayed where it was while
    everything joining it went to the canonical folder: one UUID, two
    folders."""
    kept = album_on_disk(space, "Artist", "Record", ["One"])
    odd = space.library_path / "Artist" / "Record (old rip)"
    kept[0].path.parent.rename(odd)
    joiner = _single(space, "Artist", "Two")

    combine.combine(space, albumartist="Artist", album="Record",
                    albums=[odd, joiner.path.parent], tracks=[],
                    keep=odd)

    target = space.library_path / "Artist" / "Record"
    assert sorted(p.name for p in filer.audio_in(target)) == [
        "01 - One.mp3", "Two.mp3"]
    assert not odd.exists()


def test_renumbering_makes_one_disc(space):
    """A two-disc album plus a single became disc 2 starting at track 11,
    every total 21."""
    discs = []
    for disc, title in ((1, "A"), (2, "B")):
        discs.append(filer.file_track(space, track(
            tmp_of(space), name=f"d{disc}.mp3", albumartist="Artist",
            artist="Artist", album="Double", title=title, tracknumber="1",
            discnumber=f"{disc}/2")))
    single = _single(space, "Artist", "C")
    folder = discs[0].path.parent
    order = [discs[0].path, discs[1].path, single.path]

    combine.combine(space, albumartist="Artist", album="Double",
                    albums=[folder, single.path.parent], tracks=[],
                    keep=folder, order=order)

    files = filer.audio_in(folder)
    assert sorted(p.name for p in files) == ["01 - A.mp3", "02 - B.mp3", "03 - C.mp3"]
    assert {EasyID3(p)["discnumber"][0] for p in files} == {"1/1"}


def test_the_route_counts_a_track_inside_a_chosen_folder_once(
        space, monkeypatch):
    """One single, chosen as its album and as a track, made two (R5): the
    route let a "combine" of one track through."""
    import asyncio
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app import main

    single = _single(space, "Phoebe Bridgers", "Motion Sickness")
    folder = single.path.parent
    monkeypatch.setattr(main.workspace, "for_session",
                        lambda identity, library_id: space)
    monkeypatch.setattr(main.library, "album_dir",
                        lambda identity, library_id, name: folder)
    monkeypatch.setattr(main.library, "track_path",
                        lambda identity, library_id, name: single.path)
    started = []
    monkeypatch.setattr(main.operations, "start",
                        lambda *args: started.append(args) or (
                            SimpleNamespace(as_dict=dict), True))

    body = main.CombineRequest(library_id=1, albumartist="Phoebe Bridgers",
                               album="Stranger in the Alps",
                               albums=["the folder"], tracks=["the track"])
    session = SimpleNamespace(identity=SimpleNamespace(username="alex"))
    with pytest.raises(HTTPException) as refused:
        asyncio.run(main.library_combine(body, session))
    assert refused.value.status_code == 400
    assert started == []


# --- what a combine could not do is said (2L12) ------------------------------------

def test_a_combine_reports_files_whose_identity_did_not_take(space, monkeypatch):
    """A merge whose UUID write failed on some files left a split album and
    was reported as moved."""
    singles = [_single(space, "Phoebe Bridgers", t)
               for t in ("Motion Sickness", "Scott Street")]
    monkeypatch.setattr(filer, "_write_identity", lambda *args: False)

    result = combine.combine(
        space, albumartist="Phoebe Bridgers", album="Stranger in the Alps",
        albums=[s.path.parent for s in singles], tracks=[])

    assert result["moved"] == 2
    assert len(result["failed"]) == 2
    assert all("identity tags could not be written" in f for f in result["failed"])


def test_a_combine_carries_on_past_a_filesystem_error(space, monkeypatch):
    """Only NotEditable was caught, so a full disk on one album ended the
    whole combine half done, before the scan and the summary."""
    singles = [_single(space, "Phoebe Bridgers", t)
               for t in ("Motion Sickness", "Scott Street")]
    real = filer.retag_album
    calls = []

    def full_disk_once(space_, folder, **fields):
        calls.append(folder)
        if len(calls) == 1:
            raise OSError(28, "No space left on device")
        return real(space_, folder, **fields)

    monkeypatch.setattr(filer, "retag_album", full_disk_once)

    result = combine.combine(
        space, albumartist="Phoebe Bridgers", album="Stranger in the Alps",
        albums=[s.path.parent for s in singles], tracks=[])

    assert result["moved"] == 1
    assert any("No space left" in f for f in result["failed"])
