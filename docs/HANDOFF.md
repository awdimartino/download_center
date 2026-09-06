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
their own library, staging area and beets index. It is deployed, healthy,
and has 219 tests behind a CI gate.

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

**Deployed:** commit `3bd33d5`, container healthy.

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

**Play history:** 41,203 plays imported from Last.fm covering 2022-09-18 to
2026-09-05, plus nightly snapshots from 2026-09-06 onward. Snapshots run at
local midnight (`play_day_timezone = America/New_York`) targeting the last
complete day. `GET /api/playcounts` reports whether it is up to date.

**Waiting:** 199 duplicate groups, ~15 staging items, ReplayGain at ~56%,
~62 GB reclaimable from `music_old` / `tagged_old` /
`music_backup_2026-09-04`, 30 broken `.m4a`, and Kelly has never signed in.

---

## What to do next

1. **Staging refusals.** The one thing that leaves files stuck. beets is
   configured `quiet_fallback: skip` and never guesses — but when it
   refuses, the app offers nothing to do about it. Diagnosed concretely: a
   staged "Radiohead - Let Down" with perfect tags (title, artist, album,
   track 5/12, ISRC, exact album duration) was skipped because beets found
   **five** candidate recordings and would not choose between them. So the
   fix is not better matching — it is a UI that shows the candidates and
   lets you pick, plus an *import as-is* escape hatch. See PLAN.md, "Pull
   from MusicBrainz on request".
2. **Wrapped-style stats.** Now unblocked — four years of history exist.
3. **`app/static/app.js` split** (FIXES item 29) and **`app/main.py` into
   routers** (item 28). Refactors, not bugs.

---

## Known risks in what already shipped

- **Playlist rule vocabulary is unverified end to end.** The fields the
  editor offers are Navidrome 0.58's documented criteria plus nine proven by
  existing playlists. `bpm` and `compilation` are least certain. A rejected
  field surfaces Navidrome's own error naming it, so nothing fails silently,
  but it has never been saved against the live server.
- **The frontend has no automated tests.** Every UI change this session
  shipped on a static check — ids resolve, no duplicate ids, braces balance,
  no dead selectors — and was confirmed by the user in Safari afterwards.
  Node is not available in the dev environment.
- **The health cut-down has not been seen in a browser.** The
  *show everything* toggle is new markup.

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
