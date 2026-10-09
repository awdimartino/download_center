"""The maintenance commands refuse to run as root (CODE_REVIEW L36).

`docker exec` is root by default, and a run as root left root-owned files
in /config that the app, running as its own user, could not write.
"""

from __future__ import annotations

import importlib
import sys

import pytest

COMMANDS = ["backfill", "lastfm", "reindex", "relink", "separate", "survey", "unfuse"]


@pytest.mark.parametrize("name", COMMANDS)
def test_a_command_run_as_root_stops_before_doing_anything(name, monkeypatch):
    from app import cli

    module = importlib.import_module(f"app.{name}")
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.delenv("NC_ALLOW_ROOT", raising=False)
    monkeypatch.delenv("NC_ALLOW_ROOT", raising=False)
    monkeypatch.setattr(sys, "argv", [name, "--help"])

    with pytest.raises(SystemExit) as stopped:
        module.main()

    assert "docker exec -u companion" in str(stopped.value.code)


def test_root_can_be_allowed_on_purpose(monkeypatch):
    from app import cli

    monkeypatch.setattr(cli.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setenv("NC_ALLOW_ROOT", "1")
    cli.not_as_root("survey")


def test_the_apps_own_user_is_not_stopped(monkeypatch):
    from app import cli

    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.delenv("NC_ALLOW_ROOT", raising=False)
    monkeypatch.delenv("NC_ALLOW_ROOT", raising=False)
    cli.not_as_root("survey")
