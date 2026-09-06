# Handoff

Start here in a new conversation. Read this, then [PLAN.md](PLAN.md) for
what we are building and [ARCHITECTURE.md](ARCHITECTURE.md) for how the code
works.

Written 2026-09-06. Everything below was true at that point; verify before
relying on a number.

---

## In one paragraph

Download Center is a Navidrome companion running on a Raspberry Pi. It does
what Navidrome cannot — download music, edit smart playlist rules, audit
library health, resolve duplicate copies — for any number of Navidrome
accounts, each with their own library, staging area and beets index. A long
migration to UUID-based track identity is finished. The most recent work was
a smart playlist editor and a UI pass. Everything is deployed and healthy.

---

## Ground rules the user has stated

These are standing instructions, not preferences to re-derive.

- **The Navidrome database is read-only.** Writes go through its API.
- **Original code only.** Nothing lifted from SpotTube or similar.
- **Pi access is granted, but nothing destructive.** *"you have access to the
  pi, just dont do anything destructive."*
- **Everything operates per user.** *"Alex downloads to alex library and can
  see alex's liked and stared music."*
- **Ask before starting new work.** The user asked to be consulted on
  direction rather than presented with finished features.

---

## Access

```bash
# The Pi
ssh -i ~/.ssh/id_ed25519_pi argyle@alex-pi

# Deploy: push to main, wait for the arm64 build, then
ssh -i ~/.ssh/id_ed25519_pi argyle@alex-pi \
  'cd ~/Docker && docker compose pull download-center && docker compose up -d download-center'

# Is the build done?  (gh is NOT installed locally)
curl -s "https://api.github.com/repos/awdimartino/download_center/actions/runs?per_page=1"
```

The Pi runs a prebuilt GHCR image and has no git checkout. The README now
describes this flow too; its old rsync-and-build section was removed on
2026-09-06.

Local Python is `.venv/Scripts/python.exe` — `python` is not on PATH.

---

## Where things stand

**Deployed:** commit `a4154c1`, container healthy, deployed 2026-09-06.
`state.db` is backed up beside itself before every migration
(`state.db.bak-2026-09-06`, `state.db.bak-pre-tier2`). The ledger migration
was dry-run against a copy of the real database and preserved all 825 rows.

**All bugs from the 2026-09-06 review are fixed.** 164 tests; CI runs
compile, ruff and pytest before it builds. See [FIXES.md](FIXES.md) - the
only items left open are the two refactors, which are not bugs.

**Navidrome 0.58.5.** Three libraries: `Music Library` (id 1, `/music`),
`Kelly` (id 2, `/kelly`) and `Test` (id 5, `/test`, added 2026-09-06 for
trying the per-user flow without touching real music). Users: `alex` →
library 1, `kelly` → library 2, `test` → library 5, `admin` → all three.
Note `alex` is *not* an admin account.

**Adding a library means two mounts, not one.** Navidrome and
download-center each need a bind mount at the same path — Navidrome reads
it, this writes to it. Miss the second and the app now refuses with a
message naming the path; before 2026-09-06 it filed the music inside the
container and lost it on the next pull. See ARCHITECTURE.md, "Container
boundaries".

**Library:** ~6,266 live tracks, 100% UUID-stamped, 0 duplicate UUIDs, 0
split or spanning albums. 6,623 plays recorded since 2025-11-27 across 2,477
tracks (alex 3,534; kelly 3,089).

**Do this soon:** run a **full** Navidrome scan. 49 mp3s are stamped on
disk but their UUID is not in Navidrome's index, so their plays are filed
against nothing and will be missing from every later statistic. Stamping
preserves mtime, so only a full scan re-reads them. (One `.wav` with 6 plays
can never be tracked - the format cannot hold the tag.)

**Waiting:** 199 duplicate groups, 15 staging items, ReplayGain at 56%,
~62 GB reclaimable from `music_old` / `tagged_old` /
`music_backup_2026-09-04`, 30 broken `.m4a`, and Kelly has never signed in.

---

## What to do next

The agreed order is in PLAN.md. In short:

1. ~~**Play-count snapshots.**~~ Shipped 2026-09-06. Baseline taken that
   evening: 2,429 track/user rows, alex 3,479 plays and kelly 3,089. Real
   deltas start the following day. `GET /api/playcounts` says whether it is
   still running.
2. **Last.fm backfill.** One-time, fuzzy-matched. See below.
3. **Health tab cut down** to roughly seven actionable rows.
4. ~~**Burger navigation**, replacing tabs on both phone and desktop.~~
   Shipped 2026-09-06. One overlay at every width; the bottom bar and its
   three iOS workarounds are gone. **Not yet verified in Safari** - node is
   unavailable in the dev environment, so it shipped on a static check only.
5. ~~**Tests and a CI step that runs them.**~~ Done 2026-09-06: 164 tests,
   plus ruff, all gating the image build. Grow the suite as you go.

The review backlog is clear, so this list is now the plan again. Note that
`app/static/app.js` still wants splitting (FIXES.md item 29) - the burger
rewrite is the natural moment, so do it there rather than separately.

---

## Known risks in what already shipped

- **Playlist rule vocabulary is unverified end to end.** The fields offered
  by the editor are Navidrome 0.58's documented criteria plus nine proven by
  existing playlists. `bpm` and `compilation` are least certain. A rejected
  field surfaces Navidrome's own error naming it, so nothing fails silently,
  but it has never been saved against the live server. Offered but not yet
  done: create one throwaway playlist through the service account exercising
  every field, confirm, delete.
- ~~**No tests exist.**~~ 164 now, heaviest on `duplicates.py`. It moves
  files irreversibly, so it is tested hardest. The frontend has no test
  coverage at all - verify Duplicates and Browse in Safari by hand.
- ~~**CI does not run tests, lint or type checks.**~~ A test job now gates
  the build; pull requests get the tests without the QEMU build. No linter
  or type check yet - FIXES.md item 27.

---

## Working notes

- The user reads this on a phone constantly. Mobile is a requirement.
- The user runs **Safari**, which matters: several layout bugs were WebKit
  behaviour that the local Chromium harness cannot reproduce. When something
  is WebKit-specific, say so rather than claiming a verified fix.
- Screenshots come from headless Edge. It ignores `--window-size` for the
  layout viewport, so a narrow viewport requires rendering inside a
  fixed-width iframe. A scratchpad preview harness (`preview.py`) serves the
  real app with a faked session — it is deliberately not in the repository.
- ARCHITECTURE.md ends with a list of gotchas that each cost real time. Read
  it before touching tags, beets, Navidrome's database, or CSS positioning.
