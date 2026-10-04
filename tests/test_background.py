"""The background loops survive their own failures (CODE_REVIEW M28).

The disk-audit loop caught only a failed audit; a sqlite3.Error from reading
the library list ended the task for the life of the process, and Health's
audit aged for ever.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from app import main


@pytest.mark.asyncio
async def test_the_audit_loop_survives_an_unexpected_error(monkeypatch):
    calls = []

    def roots():
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return []

    async def sleep(seconds):
        if len(calls) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(main, "_library_roots", roots)
    monkeypatch.setattr(main.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await main._audit_loop()

    assert len(calls) == 2
