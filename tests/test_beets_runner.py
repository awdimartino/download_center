"""Handing staged files to beets, and getting out of it when beets refuses.

Beets is configured never to guess, so anything it cannot place stays in
staging. That is correct and was also a dead end - nothing in the
application could file it, and the folder gave no sign that anything had
been tried. These cover the way out and the note that says why it is needed.

The audio here is a real 0.65s MP3 (tests/fixtures/silence.mp3), tagged with
mutagen. A stub will not do: mutagen returns None for a file with an ID3
header and no audio frames, so an album tag written onto one reads back as
absent - which is the exact answer the code under test is asking for.
"""

from __future__ import annotations

from pathlib import Path


from app import beets_runner

SILENCE = Path(__file__).parent / "fixtures" / "silence.mp3"


# --- which beets mode a path gets -------------------------------------------


# --- the flags that actually reach beets ------------------------------------

# --- saying why something is still sitting there ----------------------------


# --- the escape hatch, wired up ---------------------------------------------
# There are no HTTP-level tests in this suite, so these check the wiring the
# browser depends on: a route that is not registered, or one that takes the
# item by a different name than the page sends, fails only in a browser.


def _fake_subprocess(monkeypatch, stdout, returncode=0):
    seen = {}

    class Result:
        pass

    def fake_run(command, **kwargs):
        seen["command"] = command
        result = Result()
        result.returncode = returncode
        result.stdout = stdout
        result.stderr = ""
        return result

    monkeypatch.setattr(beets_runner.subprocess, "run", fake_run)
    monkeypatch.setattr(beets_runner, "ensure_config", lambda space: None)
    return seen


def _space(tmp_path):
    return type("Space", (), {"beets_dir": tmp_path,
                              "beets_library": tmp_path / "library.db"})()


def test_candidates_are_read_from_the_last_line(monkeypatch, tmp_path):
    """Beets and its plugins write to stdout as they load - fetchart alone
    prints three lines about missing API keys. Taking the whole of stdout as
    JSON would fail on every real invocation."""
    _fake_subprocess(monkeypatch, stdout=(
        "fetchart: lastfm: Disabling art source due to missing key\n"
        '{"kind": "album", "candidates": [{"id": "mb-1", "title": "Thriller"}]}\n'
    ))
    answer = beets_runner.candidates(_space(tmp_path), tmp_path / "Thriller")

    assert [c["title"] for c in answer["candidates"]] == ["Thriller"]
    assert answer["name"] == "Thriller"


def test_unreadable_match_output_is_reported_not_guessed(monkeypatch, tmp_path):
    """An empty candidate list and a broken subprocess must not look the
    same: one means "MusicBrainz has nothing", the other means "this is
    broken", and only one of them is worth acting on."""
    _fake_subprocess(monkeypatch, stdout="Traceback (most recent call last):")
    answer = beets_runner.candidates(_space(tmp_path), tmp_path / "x")

    assert answer["candidates"] == []
    assert "could not read" in answer["error"] or "form this" in answer["error"]


def test_choosing_a_release_asks_beets_rather_than_overruling_it(
        monkeypatch, tmp_path):
    """The chosen release is applied through beets' own choose_match, the
    extension point its test suite uses, which applies a match whatever the
    confidence numbers say.

    Loosening the thresholds was tried first and measured: `--search-id`
    with `strong_rec_thresh` raised *and* the `max_rec` caps lifted still
    filed nothing, because quiet mode applies only on a strong
    recommendation and missing tracks cap it at medium regardless."""
    monkeypatch.setattr(beets_runner.settings, "beets_enabled", True)
    monkeypatch.setattr(beets_runner, "_library_size", lambda space: 0)
    seen = _fake_subprocess(monkeypatch, stdout="")

    beets_runner.import_chosen(_space(tmp_path), tmp_path / "album", "mb-1")

    command = seen["command"]
    assert command[1:4] == ["-m", "app.beets_match", "--apply"]
    assert command[4] == "mb-1"


def test_a_chosen_release_that_files_nothing_says_so(monkeypatch, tmp_path):
    """Reported rather than called success. The folder is still sitting
    there either way, and "it worked" followed by an unchanged panel is the
    silence this codebase keeps producing."""
    monkeypatch.setattr(beets_runner.settings, "beets_enabled", True)
    monkeypatch.setattr(beets_runner, "_library_size", lambda space: 7)
    _fake_subprocess(monkeypatch, stdout="")

    result = beets_runner.import_chosen(_space(tmp_path), tmp_path / "a", "mb-1")

    assert result["imported"] == 0
    assert result["skipped"] == 1


# --- not asking beets the same question for ever ----------------------------
# The sweep used to retry every stuck item on every run. With a backlog of
# things that will never match, it therefore ran continuously - and one beets
# process holds the import lock, so every button in the staging tab answered
# "an import is already running". This is the fix.


# --- why an import filed nothing --------------------------------------------
# Four different failures used to arrive as one sentence, and each of them
# sends you somewhere else. A whole staging backlog was read as unmatchable
# music when the container simply had no DNS.


# --- filing a fragment as the release its siblings already use --------------

# --- the config beets is actually given -------------------------------------
# It is a string constant, so a typo in it is not a syntax error anywhere -
# it surfaces as beets quietly behaving differently on the next import.

def test_the_default_config_is_valid_yaml():
    import yaml
    rendered = (beets_runner.DEFAULT_CONFIG
                .replace("__DIRECTORY__", "/music")
                .replace("__LIBRARY__", "/library.db")
                .replace("__LOG__", "/import.log"))
    assert yaml.safe_load(rendered)["directory"] == "/music"


def _config() -> dict:
    import yaml
    return yaml.safe_load(beets_runner.DEFAULT_CONFIG
                          .replace("__DIRECTORY__", "/music")
                          .replace("__LIBRARY__", "/l.db")
                          .replace("__LOG__", "/i.log"))


def test_an_album_missing_a_track_is_still_allowed_to_file():
    """The cap this lifts was discarding albums beets had identified
    correctly. Distance still gates: measured, one track of a ten track
    release scores 0.5031 and is refused, nine of ten scores 0.0011."""
    assert _config()["match"]["max_rec"]["missing_tracks"] == "strong"


def test_extra_unrelated_tracks_are_still_capped():
    """The other half of max_rec must stay put - it is what stops a folder of
    loose tracks being filed as an album."""
    assert "unmatched_tracks" not in _config()["match"]["max_rec"]


def test_the_distance_gate_is_never_loosened():
    """max_rec caps a recommendation; strong_rec_thresh moves the gate. Only
    the first is safe to touch, and the second must stay absent."""
    config = _config()
    assert "strong_rec_thresh" not in config.get("match", {})


def test_fingerprinting_is_enabled():
    """Tag matching cannot help a file whose tags are wrong, which most
    hand-dropped rips are."""
    assert "chroma" in _config()["plugins"]


def test_musicbrainz_matches_are_not_penalised_for_their_source():
    """Adding chroma made beets count two metadata source plugins, which
    switched on a data_source penalty against every track - the files carry
    no data_source tag, so each one scored a mismatch. Measured on a 24 file
    folder against the 24 track release, titles and track numbers exact:
    0.0003/strong without chroma, 0.1113/medium with it, 0.0002/strong with
    this set. The penalty read belongs to the source being matched against,
    so it is musicbrainz's that has to be zero."""
    import yaml
    config = yaml.safe_load(beets_runner.DEFAULT_CONFIG
                            .replace("__DIRECTORY__", "/music")
                            .replace("__LIBRARY__", "/l.db")
                            .replace("__LOG__", "/i.log"))
    assert config["musicbrainz"]["data_source_mismatch_penalty"] == 0


# --- the path template an inherited config carries ---------------------------
#
# `ensure_config` never overwrites a config and `adopt_legacy` rewrites only
# the three absolute paths inside one, so a workspace that inherited the
# single-user installation kept filing with its template:
#
#     $albumartist/$album%aunique{} ($original_year)/$track $title
#
# Both %aunique{} and the year are exactly what the current template's own
# comment says must not be there, and the folder no longer matches what the
# filer writes - so a confirmed retag moves files out of the frozen layout
# and leaves the album's real directory behind, empty.

LEGACY_CONFIG = """directory: /music
library: /config/beets/library.db

paths:
  default: $albumartist/$album%aunique{} ($original_year)/$track $title
  singleton: Singles/$artist - $title
  comp: Various Artists/$album%aunique{} ($original_year)/$track $title

plugins: musicbrainz
"""


def _real_space(tmp_path, monkeypatch):
    """A genuine Workspace on disk, unlike the stub `_space` above - these
    tests read and write a real config file."""
    from app import workspace
    from app.config import settings

    monkeypatch.setattr(settings, "output_dir", tmp_path / "untagged")
    monkeypatch.setattr(workspace, "CONFIG_DIR", tmp_path / "config")
    (tmp_path / "music").mkdir(exist_ok=True)
    space = workspace.Workspace(username="alex", library_id=1,
                                library_name="Music",
                                library_path=tmp_path / "music")
    space.prepare()
    return space


def test_an_inherited_path_template_is_brought_up_to_date(tmp_path,
                                                          monkeypatch):
    space = _real_space(tmp_path, monkeypatch)
    space.beets_config.write_text(LEGACY_CONFIG, encoding="utf-8")

    beets_runner.ensure_config(space)
    text = space.beets_config.read_text(encoding="utf-8")

    default = next(line for line in text.splitlines()
                   if line.strip().startswith("default:"))
    assert "%aunique{}" not in default
    assert "$original_year" not in default
    assert "$track - $title" in default


def test_repairing_the_paths_leaves_everything_else_alone(tmp_path,
                                                          monkeypatch):
    """`directory` is where this person's music goes, and rewriting it would
    file their library somewhere else entirely."""
    space = _real_space(tmp_path, monkeypatch)
    space.beets_config.write_text(LEGACY_CONFIG, encoding="utf-8")

    beets_runner.ensure_config(space)
    text = space.beets_config.read_text(encoding="utf-8")

    assert "directory: /music" in text
    assert "library: /config/beets/library.db" in text
    assert "plugins: musicbrainz" in text


def test_a_config_already_current_is_not_rewritten(tmp_path, monkeypatch):
    space = _real_space(tmp_path, monkeypatch)
    beets_runner.ensure_config(space)
    before = space.beets_config.read_text(encoding="utf-8")

    assert beets_runner._repair_paths(space) is False
    assert space.beets_config.read_text(encoding="utf-8") == before


def test_a_config_with_no_paths_block_is_left_alone(tmp_path, monkeypatch):
    """Hand-edited into a shape this does not recognise. Guessing at it is
    worse than leaving it."""
    space = _real_space(tmp_path, monkeypatch)
    space.beets_config.write_text("directory: /music\n", encoding="utf-8")

    assert beets_runner._repair_paths(space) is False
    assert space.beets_config.read_text(encoding="utf-8") == "directory: /music\n"


def test_the_repaired_template_matches_the_filers_layout(tmp_path,
                                                         monkeypatch):
    """The point of the repair. Beets and the filer have to agree about where
    an album lives, or a retag moves the files somewhere else."""
    space = _real_space(tmp_path, monkeypatch)
    space.beets_config.write_text(LEGACY_CONFIG, encoding="utf-8")
    beets_runner.ensure_config(space)

    text = space.beets_config.read_text(encoding="utf-8")
    assert "%if{$albumartist,$albumartist," in text
    assert "%if{$album,$album,Unknown Album}/$track - $title" in text
