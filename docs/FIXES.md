# Fix list

Everything found by the review on 2026-09-06, tracked to completion. Ordered
by magnitude, not by effort. Tick items as they land; do not delete them —
a closed item is the record that it was looked at.

Narrative context for the top four is in [PLAN.md](PLAN.md) under
*Found by review*. How the code works is in
[ARCHITECTURE.md](ARCHITECTURE.md).

Status key: `[ ]` open · `[x]` done · `[~]` partly done · `[?]` needs a
decision from the user before it can be done.

---

## Tier 1 — loses or corrupts data

- [x] **1a. Duplicate resolve has no confirmation.**
      `app/static/app.js`, `renderGroup`. Bulk auto-resolve confirms; playlist
      delete confirms; the per-group button that moves a file out of the
      library does not.
- [x] **1b. Quarantine flattens album structure.**
      `duplicates._quarantine_dir()` / `resolve()` use `source.name`, so two
      albums' `01 Intro.mp3` collide and become `... (2).mp3`. Recovery is
      guesswork.
- [x] **1c. Nothing records what was quarantined.** No ledger row, no
      source→target log. Undo is archaeology.
- [x] **1d. `postDupe` discards the response.** A failed move or an
      unmigrated star is never shown; the group just silently persists.
- [x] **1e. Quarantine directory was outside every mounted volume.**
      Filed as "not per-library", and it was worse than that.
      `settings.music_dir.parent / "duplicates-removed"` resolves to
      `/duplicates-removed`. The container mounts `/config`, `/downloads`,
      `/music`, `/kelly` and `/navidrome` — confirmed against the running
      container on 2026-09-06 — and not that. So a quarantined file was
      *copied* into the container's own writable layer (the move crossed a
      device boundary, so the original was unlinked) and destroyed by the
      next `docker compose pull`. "Nothing is deleted" was false in the
      deployed layout.
      **Operational follow-up: waived.** The user does not need the files
      already in the old `/duplicates-removed`; they go with the next deploy
      and that is accepted (decided 2026-09-06).
- [x] **2. Duplicate review UI never shows each copy's title** — only the
      group's first. `normalise()` strips `feat.` clauses, so two different
      collaborations group together and render as identical rows.
- [x] **3a. The ledger is global, not per-user.** Now keyed on
      `(source_id, library_id)`. Decided 2026-09-06: scoped to a **library**,
      not to a person — the question is "is this recording already in this
      collection", and a collection is a library. Existing rows backfilled to
      library 1, which is where they were all downloaded.
- [x] **3b. No path ever removes a ledger row.** `ledger.forget()`, exposed
      as `POST /api/ledger/forget` and reachable by clicking the "already
      have" tag in Browse. Deliberately *not* automatic on duplicate
      resolution: that keeps a copy, so the track is still held.
- [x] **4. `health.py` `indexed_stamped` query is broken and silently
      suppressed.** `select count(*) from media_file` with no `mf` alias while
      the WHERE clause says `mf.missing`; raises `sqlite3.Error`, swallowed by
      a bare `contextlib.suppress`. "Stamped but not yet scanned" has never
      fired.

## Found later

- [x] **30. A library Navidrome knows about but this container cannot see
      was written to anyway.** Found 2026-09-06 while adding a Test library.
      `for_session` built a workspace from Navidrome's recorded path without
      checking it was mounted here, so beets was configured with a
      `directory:` that existed only inside the container. It would create
      it in the writable layer, file the music, report success, and put the
      tracks in the ledger — so nothing ever asked for them again, and the
      next `docker compose pull` destroyed them.
      Same shape as 1e. `workspace.require_mounted` refuses it now, on by
      default; read-only callers pass `require_library=False`.

## Tier 2 — the app stalls under ordinary use

- [x] **5. Long blocking work inside request handlers, behind process-wide
      locks.** `/api/staging/import` holds `beets_runner._import_lock` for up
      to 900s *per path*; `/api/health/audit` holds `diskaudit._lock` for a
      whole library walk. Both are one button-press away, neither dedupes.
      Both are jobs wearing a request's clothing.
- [x] **5b. `diskaudit.refresh` docstring claims the opposite of what it
      does** — "Concurrent callers share one walk." They serialise, each
      doing a full walk.
- [x] **6a. The websocket pushes the whole job dict at 2 Hz.**
      `worker._pusher`. A 200-track playlist re-serialises everything twice a
      second to a phone.
- [x] **6b. The browser rebuilds every card and row on each message.**
      `app.js render()` → `replaceChildren`.
- [x] **6c. One slow websocket client stalls publishes for everyone.**
      `Broker.publish` awaits `send_json` per client sequentially.
- [x] **7a. The concurrency semaphore is per-job.** `worker.run_job` creates
      it inside the job, so N jobs give N × `concurrency` downloads, and
      `rate_limit_sleep` is likewise per-job.
- [x] **7b. Nothing bounds job creation, resolved playlist size, or `JOBS`.**
      `JOBS` grows for the process lifetime and is fully re-serialised on
      every `GET /api/jobs`.
- [x] **8. `matcher._search` swallows every exception**, turning a transient
      429 into `MatchError("No results returned")` — which `worker` then
      deliberately never retries, on the reasoning that an identical search
      returns identical results. True for a miss, wrong for a rate limit.
- [x] **8b. `matcher._client` is a single shared `YTMusic`** (wrapping a
      non-thread-safe `requests.Session`) used from `concurrency` threads.

## Tier 3 — correctness

- [x] **9a. `sanitize()` does not strip backslashes.** `staging.py`
      `r'[<>:"/\|?*\x00-\x1f]'` — inside a character class `\|` escapes the
      pipe, so backslash never joins the set. Verified.
- [x] **9b. `[` and `]` survive sanitising**, breaking
      `downloader.download`'s `glob(stem + ".*")` diagnostic fallback.
- [x] **10. `_move_into_place` silently overwrites after 98 collisions**,
      despite its docstring saying it never does. `staging.py`.
- [x] **11. `Workspace.key` does filesystem I/O in a property** and changes
      its answer if the `.owner` marker changes — silently abandoning a
      staging area and beets index, the exact failure it exists to prevent.
- [x] **12. beets success is detected by string-matching `"Skipping"`** in
      human-readable output. Stamping and `navidrome.notify()` are both gated
      on the result.
- [x] **13. `to_form` accepts operators `to_rules` will reject**, so a
      playlist can open as editable and then fail on save with an error about
      a rule the app itself supplied.

## Tier 4 — smaller

- [x] **14. `GET /api/settings` is not admin-gated** and returns
      `navidrome_url`, `navidrome_user` and `spotify_client_id` to any signed-in
      account.
- [x] **15. Sessions are never pruned.** `auth._sessions` evicts only on
      presentation of that exact cookie; expired identities hold live
      Navidrome bearer tokens for a fortnight.
- [x] **16. `EDITABLE` includes `beets_enabled`; `SettingsUpdate` does not.**
      Returned by GET, unsettable by PUT. Currently inert — no form control.
- [x] **17. `config.save()` rewrites `config.toml` with only `EDITABLE`
      keys**, dropping any hand-set `staging_quiet_seconds` (which has no env
      override either).
- [x] **18. `pyyaml` is imported but not in `requirements.txt`.** Arrives
      transitively via beets; the `except Exception` around it degrades an
      ImportError into filing music at the wrong root.
- [x] **19. `tagger._fetch_cover` uses `urllib.request.urlopen`** on an
      untrusted URL, which honours `file://` and `ftp://`.
- [x] **20. Cancelling during the resolve phase returns 409.**
      `RUNNING[job_id]` is not set until `_run`, but the job spends its whole
      resolve phase in `_resolve_job`.
- [x] **20b. `retry_job` lets `workspace.for_session`'s `ValueError` escape
      as a 500** where `delete_job` maps it to 400.
- [x] **21. `orphan_annotations` is scoped to neither user nor library.**
      Documented in ARCHITECTURE.md as of 2026-09-06 rather than fixed.
- [x] **22. `rg_track_gain != 0` counts a legitimate 0 dB gain as
      unmeasured.** `health.py`.
- [x] **23. `/api/search?limit=` is unbounded** and passed straight to
      Spotify; over 50 returns a 400 surfaced as a 502.
- [x] **24. No `secure` flag on the session cookie**, and sign-in posts a
      Navidrome password over plain HTTP. Defensible on a LAN; documented in
      the README as of 2026-09-06. Revisit if this is ever exposed.

## Cross-cutting

- [x] **25. Tests.** None existed. Seeded alongside fix 1; the list to grow
      is in PLAN.md under *Tests — the real gap*.
- [x] **26. CI runs no tests, lint or type check.** A test job must run
      before the four-minute QEMU build.
- [x] **27. No linter or formatter config exists.**
- [ ] **28. `app/main.py` is 939 lines**; split into routers.
- [ ] **29. `app/static/app.js` is 1,215 lines in one global scope.** The
      burger rewrite is the natural moment to split it per panel.

---

## The pattern

Every Tier 1 item fails **silently** — a bare `suppress`, an
`except Exception: return []`, string-matched subprocess output, a discarded
response body. Prefer fixes that make a failure *visible* over fixes that
merely make it less likely.

---

## Per-user questions

Answered 2026-09-06. Both items are done; the answers are recorded on them
above and in PLAN.md's decisions log.

---

## Set aside — the read-only view

`GET /api/duplicates/quarantined` and the collapsible section at the bottom
of the Duplicates tab list what has already been removed. It walks each
visible library's `duplicates-removed/` and joins the ledger onto it, so
both disagreements show: a file with no record (moved by an older version or
by hand) and a record whose file has gone.

Read-only on purpose. Putting a copy back means deciding what to do about
the one that was kept, and that is not a decision to make from a list.
