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
    seen = _fake_subprocess(monkeypatch, stdout="")

    beets_runner.import_chosen(_space(tmp_path), tmp_path / "album", "mb-1")

    command = seen["command"]
    assert command[1:4] == ["-m", "app.beets_match", "--apply"]
    assert command[4] == "mb-1"


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




# --- a retag is not an import ------------------------------------------------
#
# Success used to be read from beets' item count growing, which is what a
# first import does and what a *re*-tag never does: beets already has these
# files. So every retag reported "the album is unchanged", the album UUID was
# never re-pointed, and the registry went on describing an album that no
# longer existed under that name.

def test_a_retag_is_judged_by_what_beets_returned(monkeypatch, tmp_path):
    monkeypatch.setattr(beets_runner.settings, "beets_enabled", True)
    _fake_subprocess(monkeypatch, returncode=0,
                     stdout='plugin chatter\n{"applied": true, "chosen": "mb-1"}\n')

    result = beets_runner.import_chosen(_space(tmp_path), tmp_path / "a", "mb-1")

    assert result["imported"] == 1
    assert result["chosen"] == "mb-1"


def test_a_retag_beets_refused_is_reported_as_a_failure(monkeypatch, tmp_path):
    """Reported rather than called success. "It worked" followed by an
    unchanged panel is the silence this codebase keeps producing."""
    monkeypatch.setattr(beets_runner.settings, "beets_enabled", True)
    _fake_subprocess(monkeypatch, stdout="no matching release", returncode=1)

    result = beets_runner.import_chosen(_space(tmp_path), tmp_path / "a", "mb-1")

    assert result["imported"] == 0
    assert result["failed"]


def test_beets_is_told_not_to_move_anything():
    """The filer is the only thing that decides where a track lives. With
    move and copy off, beets' path template never writes a path - which is
    why there is no longer any code keeping that template in step."""
    import yaml

    config = yaml.safe_load(beets_runner.DEFAULT_CONFIG)
    assert config["import"]["move"] is False
    assert config["import"]["copy"] is False
    assert config["import"]["write"] is True


# An older workspace config says `move: yes`, and `ensure_config` never
# rewrites one. Applying a match must not let that through: beets carried the
# files into its own layout, the filer found nothing at the old folder, and
# the retag reported success (CODE_REVIEW C1). Run in a subprocess because
# beets' config is a process-wide singleton read from BEETSDIR.

_APPLY_PROBE = """
import json, sys
from pathlib import Path
from beets import config, importer
from app import beets_match

seen = {}
def run(self):
    seen.update({k: config["import"][k].get() for k in ("move", "copy", "write")})
importer.ImportSession.run = run
beets_match.apply_choice(Path(sys.argv[1]), "mb-1")
print(json.dumps(seen))
"""


def test_applying_a_match_never_moves_files_whatever_the_config_says(tmp_path):
    import json
    import os
    import subprocess
    import sys

    beets_dir = tmp_path / "beets"
    beets_dir.mkdir()
    (tmp_path / "album").mkdir()
    (beets_dir / "config.yaml").write_text(
        f"directory: {(tmp_path / 'music').as_posix()}\n"
        f"library: {(beets_dir / 'library.db').as_posix()}\n"
        "import:\n  move: yes\n  copy: yes\n  write: no\n"
        "plugins: []\n", encoding="utf-8")
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-c", _APPLY_PROBE, str(tmp_path / "album")],
        cwd=root, capture_output=True, text=True, timeout=120,
        env={**os.environ, "BEETSDIR": str(beets_dir), "PYTHONPATH": str(root)})
    assert result.returncode == 0, result.stderr
    seen = json.loads(result.stdout.strip().splitlines()[-1])
    assert seen == {"move": False, "copy": False, "write": True}


# beets_match exits 0 and prints `"applied": false` when the chosen release
# was not among beets' candidates. That used to be reported as a retag, and
# the album left the review queue for good (CODE_REVIEW H1).

def test_a_release_beets_did_not_apply_is_not_a_retag(monkeypatch, tmp_path):
    monkeypatch.setattr(beets_runner.settings, "beets_enabled", True)
    _fake_subprocess(monkeypatch, returncode=0,
                     stdout='{"applied": false, "chosen": "mb-1"}\n')

    result = beets_runner.import_chosen(_space(tmp_path), tmp_path / "a", "mb-1")

    assert result["imported"] == 0
    assert result["skipped"] == 1
    assert not result["failed"]


def test_an_unreadable_apply_answer_is_a_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(beets_runner.settings, "beets_enabled", True)
    _fake_subprocess(monkeypatch, stdout="", returncode=0)

    result = beets_runner.import_chosen(_space(tmp_path), tmp_path / "a", "mb-1")

    assert result["imported"] == 0
    assert result["failed"]
