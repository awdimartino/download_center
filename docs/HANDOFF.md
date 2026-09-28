# Handoff

Start here in a new conversation. Read this, then [PLAN.md](PLAN.md) for
what we are building, [ARCHITECTURE.md](ARCHITECTURE.md) for how the code
works, and [FIXES.md](FIXES.md) for what a review found and what is left.

Rewritten 2026-09-06. Everything below was true at that point; verify before
relying on a number.

---

## In one paragraph

Download Center is a Navidrome companion running on a Raspberry Pi. It does
what Navidrome cannot — download music, edit smart playlist rules, audit
library health, resolve duplicate copies, and keep the listening history
Navidrome throws away — for any number of Navidrome accounts, each with
their own library, working directory and beets index. It is deployed,
healthy, and has a test suite behind a CI gate.

---

## Ground rules the user has stated

Standing instructions, not preferences to re-derive.

- **The Navidrome database is read-only.** Writes go through its API.
- **Original code only.** Nothing lifted from SpotTube or similar.
- **Pi access is granted, but nothing destructive.** *"you have access to the
  pi, just dont do anything destructive."*
- **Everything operates per user.** *"Alex downloads to alex library and can
  see alex's liked and stared music."*
- **Ask before starting new work.** Consult on direction rather than
  presenting finished features.
- **Fix bugs before adding features.** *"i want all the bugs fixed before we
  start adding new stuff."*
- **Do not build infrastructure for one-time jobs.** Throwaway scripts, run
  and deleted.

---

## Access

```bash
ssh -i ~/.ssh/id_ed25519_pi argyle@alex-pi

# Deploy: push to main, wait for CI, then
ssh -i ~/.ssh/id_ed25519_pi argyle@alex-pi \
  'cd ~/Docker && docker compose pull download-center && docker compose up -d download-center'

# Is the build done?  (gh is NOT installed locally)
curl -s "https://api.github.com/repos/awdimartino/download_center/actions/runs?per_page=1"
```

The Pi runs a prebuilt GHCR image and has no git checkout. Local Python is
`.venv/Scripts/python.exe` — `python` is not on PATH.

**Back up `state.db` before any deploy that migrates it**, while the
container is stopped. The `state.db.bak-*` files on the Pi are from exactly
that.

---

## Where things stand

**2026-09-28: feedback-round Sessions 1 and 2 are committed but NOT deployed.**
Next up is Session 3 in PLAN.md. Before deploying:
- It needs a fresh image, not just a restart: the Dockerfile now installs
  `rsgain`, and `requirements.txt` gained `python-multipart` (Session 2's
  upload endpoints need it to parse the request at all - without it the
  container starts and `/api/inbox/upload` 500s on the first request). Push
  to `main`, wait for CI, then pull on the Pi.
- It adds the `album_reviewed` table to `state.db` (created automatically,
  nothing existing changes); back up `state.db` anyway.
- Nothing in it has been seen in a browser (Safari): the sticky
  `.library-status` bar, the playlist library checkboxes, the Library
  filter dropdown, ReplayGain progress/Stop, and the whole Drop tab -
  particularly dragging a folder in on desktop Safari/Chrome, since iOS
  Safari has no folder picker and was only exercised through the plain
  multi-file input and an HTTP-level check standing in for a browser (see
  ARCHITECTURE.md's inbox section). Check all of it after the deploy.
- Existing smart playlists stay unscoped (drawing from every library)
  until each is opened and saved; their cards say so.

**Deployed:** see `git log` - the Pi runs the GHCR image built from `main`
(`docker compose pull download-center && docker compose up -d
download-center`). Container healthy. Verified after the pull:
the shell is `no-store` and stamps `?v=<hash>` onto its assets, `/static`
answers `no-cache`, and the served `app.js` contains the *Import as-is*
button. Sessions are in memory, so a restart signs everyone out — a
websocket 403 in the log straight after a deploy is that, not a fault.

**A deploy did not reach the browser before `21ab5b6`** (FIXES item 39).
Static assets carried no `Cache-Control`, so a fresh shell loaded a stale
`app.js` and the first report of the escape hatch was that it was not
there. If a change ever appears to be missing, check the asset version in
the page source against `asset_version()` before doubting the code.

**Navidrome 0.58.5.** Three libraries: `Music Library` (id 1, `/music`),
`Kelly` (id 2, `/kelly`), `Test` (id 5, `/test`). Users: `alex` → 1,
`kelly` → 2, `test` → 5, `admin` → all. Note `alex` is *not* an admin.

**Adding a library means two bind mounts, not one** — Navidrome and
download-center, at the same path. Miss the second and the app now refuses
with a message naming the path; before 2026-09-06 it filed the music inside
the container and lost it on the next pull.

**Library:** ~6,270 live tracks, 100% UUID-stamped. 125 `media_file` rows
whose files are gone (deleted during the migration) sit in folders Navidrome
has flagged missing — harmless, and excluded from every query that matters.

**Library, 2026-09-26:** 7,807 tracks. Album identity is clean - `python -m
app.survey` reports 0 split and 0 fused, and every album has a registry
row. `app/unfuse.py` is what got it there; `app/backfill.py` records the
albums whose files already agree.

**Play history:** 41,203 plays imported from Last.fm covering 2022-09-18 to
2026-09-05, plus snapshots from 2026-09-06 onward. Snapshots were nightly
until 2026-09-26 and are now read every 5 minutes, which recovers each
play's own timestamp from Navidrome's `play_date` - see `play_snapshot` in
ARCHITECTURE.md. Days are bucketed in `play_day_timezone`
(`America/New_York`). `GET /api/playcounts` reports whether the collector is
alive, which since the cadence change means "read within the last 30
minutes" rather than "yesterday was captured".

The 38,559 imported rows are still day-granular. Last.fm holds a timestamp
per scrobble and the retired importer already fetched them
(`git show 6a9ff5e^:app/lastfm.py`, `plan()` collapsed them at the
`strftime("%Y-%m-%d")`), so that history is backfillable with a fresh API
key.

**Staging, as of the deploy:** alex 14 albums + 1 single, kelly 3 singles,
test 1 single — `Radiohead - Let Down.mp3`, the file the escape hatch was
diagnosed on. Every one of them has an *Import as-is* button now, and gains
a reason on the row after the next sweep (≤30 min; the notes are in memory,
so a restart clears them until it runs again).

**Waiting:** 199 duplicate groups, ReplayGain at ~56%,
~62 GB reclaimable from `music_old` / `tagged_old` /
`music_backup_2026-09-04`, 30 broken `.m4a`, and Kelly has never signed in.

---

## What to do next

As of 2026-09-26 nothing is broken. What is left is judgement, polish and
one thing only the user can supply.

1. **~296 duplicate groups to review.** The only health warning, and it
   cannot be automated away: `confident` requires a shared MusicBrainz id
   and none of these have one. The last survey of 240 broke down as 183
   same-album with no clearly better copy, **49 that are a single beside
   its album and not duplicates at all**, 5 whose lengths differ enough to
   be different mixes, and 3 genuinely resolvable. Do not bulk-resolve
   while music is being filed - importing is what creates duplicates, so a
   survey taken beforehand is stale by the time it finishes.
2. **`config/cookies.txt`.** Downloads 403 without it. `yt-dlp` is already
   pinned to the current release, so bumping fixes nothing; the file has to
   be exported from a browser and dropped in, and
   [downloader.py](../app/downloader.py) picks it up automatically.
3. **The candidate picker.** The other half of the beets refusal work.
   *Import as-is* shipped, so nothing is stuck, but there is still no way
   to see the candidate recordings beets would not choose between and point
   at one. See PLAN.md, "Pull from MusicBrainz on request".
4. **A sessions view.** The data exists since play counts began being read
   every five minutes - 4,080 sessions, the longest 8h14m - and nothing
   displays it. Belongs in the Listening panel; Home is deliberately thin.
5. **5,572 imported plays are still day-granular**, because those tracks'
   artist tags changed since the September import so the scrobble text no
   longer matches. `python -m app.lastfm <user> --times` after any tag
   cleanup picks up more; it skips rows that already have a time.
6. **`app/static/app.js` split** (FIXES item 29) and **`app/main.py` into
   routers** (item 28). Refactors, not bugs.
7. **Beets vs Picard**, still open from the design doc.

---

## Known risks in what already shipped

- **Playlist rule vocabulary is unverified end to end.** The fields the
  editor offers are Navidrome 0.58's documented criteria plus nine proven by
  existing playlists. `bpm` and `compilation` are least certain. A rejected
  field surfaces Navidrome's own error naming it, so nothing fails silently,
  but it has never been saved against the live server.
- **The frontend has no *executing* tests.** That static check — ids
  resolve, no duplicate ids, no dead selectors — is now
  `tests/test_frontend.py` and runs in CI, so a renamed id or a menu entry
  with no panel behind it fails the build. Nothing executes `app.js`: Node
  is not available here or in CI. Layout and behaviour still need Safari.
- **The health cut-down has not been seen in a browser.** The
  *show everything* toggle is new markup.
- **Neither has the Staging tab's new row.** Each item now carries a reason
  and an *Import as-is* button in a fourth grid cell, which drops to its own
  full-width row under 640px. Static checks and the Python side are covered;
  the layout is not.
- **A refusal note is lost on restart.** It is held in memory on purpose —
  it describes the last attempt, not the library — so after a container
  restart a stuck item shows no reason until the next sweep runs. Absent, not
  wrong, but do not read "no note" as "never tried".

---

## Working notes

- The user reads this on a phone constantly. Mobile is a requirement.
- The user runs **Safari**, which matters: several layout bugs were WebKit
  behaviour that a Chromium harness cannot reproduce. When something is
  WebKit-specific, say so rather than claiming a verified fix.
- **Verify against real data before deploying.** Copy `navidrome.db` or
  `state.db` down and run the new code against it. That caught a ledger
  migration, a play-count query that counted deleted tracks, and a Last.fm
  matcher missing four thousand plays. Delete the copies afterwards — they
  contain the user's listening.
- ARCHITECTURE.md ends with gotchas that each cost real time. Read it before
  touching tags, beets, Navidrome's database, container paths, async jobs,
  or CSS positioning.
- **The recurring bug in this codebase is silent failure**: a bare
  `suppress`, an `except Exception: return []`, a flag argparse accepts and
  nothing reads, a check whose SQL never ran, a day that writes no rows and
  so never counts as done. Prefer a fix that makes the failure visible over
  one that makes it less likely.
