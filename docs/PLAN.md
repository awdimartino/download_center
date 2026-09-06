# Download Center — what it is and where it is going

Working document. The repository copy is the source of truth; if something
here disagrees with the code, the code is what shipped and this is what we
meant. Updated as work lands.

Last updated: 2026-09-06.

---

## What this is

> Do what Navidrome can't, in a browser, from a phone or a computer.

Two halves, both first-class:

**The gaps.** Things Navidrome has no answer for. Smart playlist rules it
will only read from a `.nsp` file. Downloading music at all. Listening
history it throws away. If Navidrome does something well, this should not
do it worse — that is why the playlist editor covers rules and not track
ordering.

**Library maintenance.** The work otherwise done over SSH: dedupe, tag
audit, ingest, and — not yet built — editing metadata by hand, pulling a
release from MusicBrainz on request, indexing and removing tracks.

Everything is reachable on a phone. That is a requirement, not a nicety:
the maintenance jobs are exactly the ones you want to trigger from the
sofa.

### Rules this implies

1. **Navidrome's database is read-only.** Every write goes through its API,
   as the signed-in user. There is no second copy of who exists, who can see
   what, or what is starred — a second copy would eventually disagree, and
   the disagreement would be invisible until it mattered.
2. **Everything is per-user.** Which library a download lands in, whose
   stars a duplicate carries, whose playlists these are. Derived from
   Navidrome's own account records, never configured here.
3. **Nothing is deleted.** Files are moved to a quarantine directory. This
   already holds for duplicates and must hold for track deletion when it
   arrives.
4. **Original code.** No lifting from SpotTube or similar.
5. **Identity is the UUID.** `navidrome_uuid` on the file survives re-tags,
   moves and a rebuilt database. Anything that needs to refer to a track
   across time refers to it by UUID.

---

## In flight

Nothing. The playlist editor shipped and is deployed.

See also [ARCHITECTURE.md](ARCHITECTURE.md) for how the code works and
[HANDOFF.md](HANDOFF.md) for picking this up in a new conversation.

---

## Next

### 1. Play-count tracking

**Why it can't wait.** Navidrome's `annotation` table stores a *cumulative*
`play_count` and only the *most recent* `play_date`. There are 6,623 plays
recorded since 2025-11-27 and no way to ask what was played in March. Every
day without snapshots is a day of history that cannot be recovered later.
This is the only item on the list with a clock on it.

**Design**

- A nightly job reads Navidrome's database (read-only) and writes to this
  app's own `state.db`.
- Rows are keyed by **track UUID and user**, not `media_file.id`.
- **Only changed rows are stored.** A full capture is ~2,477 rows; most
  nights only a few dozen tracks are played. Changed-only keeps a year in
  the tens of thousands rather than ~900k.
- Daily plays are the difference between consecutive snapshots.
- **Negative deltas are an anomaly, not a number.** A re-import or a counter
  reset can lower a count; that gets recorded as such rather than emitted as
  minus-four plays.
- The first night is a baseline. Real data starts on the second.

**Last.fm's place.** It has genuine per-scrobble timestamps going back
further than any snapshot could, so it is the better source for *history*.
But it matches by artist and title rather than UUID, and it is connected for
one account. Kelly has 3,089 plays. Snapshots cover both users uniformly and
depend on nothing external. Use Last.fm to backfill, snapshots as the
ongoing truth.

### 2. Last.fm backfill — one time only

Snapshots start history from tonight. Last.fm already holds the part that
came before, with genuine per-scrobble timestamps, and importing it once
gives the stats something to say on day one instead of in a month.

**Why the matching should work.** Last.fm stores the artist and track name
the scrobbler sent — it does not resolve them to a MusicBrainz entity. Those
strings came from the tags on these files. So both sides of the match are
our own metadata, which is a far easier problem than matching against a
third party's catalogue.

**Design**

- Run once, by hand, per user. Not a background job.
- Fetch the full scrobble history from Last.fm's API, then match each
  scrobble to a track UUID by normalised artist and title, with duration as
  a tie-breaker where several tracks match.
- **Report before writing.** Matched, ambiguous and unmatched counts, with
  a sample of each. A backfill that quietly matched 60% would poison every
  statistic built on it afterwards.
- Store scrobbles with their real timestamps, in the same table shape as
  snapshot-derived plays but flagged as imported — so a later question can
  ask about either source, and the join between them is visible rather than
  assumed.
- Idempotent: running it twice must not double anyone's history.

Only alex's account is connected to Last.fm. Kelly's 3,089 plays have no
history to import, which is the argument for snapshots carrying the ongoing
record for both.

### 3. Health tab — cut it down

Decided: **keep only what can be acted on.** There are 21 checks in six
sections today, several asking the same question twice — once of Navidrome's
database and once of the disk — and a whole tier that exists because of a
migration that is finished.

| Check | Proposed | Why |
|---|---|---|
| Tracks with no ReplayGain | **keep** | 56% covered; there is a job to run |
| Files Navidrome can no longer find | **keep** | Real, and stars hang off them |
| Stars and ratings pointing nowhere | **keep** | Real and fixable |
| Unreadable files | **keep** | 30 broken `.m4a` waiting |
| Tracks with no MusicBrainz id | **keep** | Feeds the MusicBrainz tooling below |
| Free space | **keep** | A fact, but one you act on |
| Tracks with no UUID (database) | **merge** | Same question as the disk check |
| Files with no UUID (disk) | **merge** | The disk is the authority; keep one row |
| Duplicate UUIDs (database) | **merge** | Same question asked twice |
| UUIDs on more than one file (disk) | **merge** | Keep one row |
| Directories with two album UUIDs | **demote** | Migration artifact; can recur, rarely |
| Album UUIDs spread across directories | **demote** | As above |
| Tracks with no album | **cut** | Low value, never acted on |
| Tracks numbered zero | **cut** | Low value, never acted on |
| Albums / singles awaiting attention | **cut** | The Staging tab is this, in detail |
| Oldest staged item | **cut** | As above |
| Audio files (count) | **cut** | A fact, not a health check |
| Last scan | **cut** | Status, not health |
| Uptime | **cut** | Status, not health |
| Audit (pending) | **keep as control** | It is a button, not a metric |

Roughly seven rows instead of twenty-one. "Demote" means behind a
*show everything* toggle rather than deleted — they can recur.

Explicitly **not** cut: the Browse and Staging tabs. The problem with health
was too many checks, not the wrong idea.

### 4. Navigation — burger, both sizes

Decided: **one pattern everywhere.** A menu button opening a full overlay,
identical on phone and desktop. Costs two taps per navigation; buys room to
grow past six items and one layout to maintain instead of two.

This replaces the top tabs and the bottom bar. It also removes the reason
the tab labels need short forms, and the badge-position workarounds that
came with them.

Carries over: the counts (Health, Duplicates) need to stay visible without
opening the menu — an unread-style dot on the button when anything needs
attention.

---

## Later

### Library maintenance tools

New, from this conversation. Roughly in order of how much they need
designing:

- **Manual metadata editing.** Edit tags on a track or album from the
  browser. Needs care: the file is the truth, Navidrome is a cache, and a
  write has to be followed by a rescan or the two disagree.
- **Pull from MusicBrainz on request.** Point at an album, search MusicBrainz,
  apply the chosen release. This is beets' matcher, driven by hand instead of
  by confidence thresholds — for the cases where beets refused to guess.
- **Index tracks.** Trigger a scan of a path without waiting for the sweep.
- **Delete tracks.** Remove from the library. Must quarantine, never
  `unlink` — the same rule the duplicates flow already follows.

### Wrapped-style stats

Blocked on play-count tracking having run for long enough to say anything.
Sensible once there are a few weeks of snapshots.

---

## Code quality

Deliberately listed after the features: these are risk and speed, not
comfort — but nothing here is on fire.

### Tests — the real gap

There are **none**. 5,522 lines of Python, and `duplicates.py` moves files
irreversibly. Every check run while building the playlist editor — the rule
round-trip against the six real playlists, the validation matrix, the
browser test proving two Saves make one playlist — lived in a scratchpad and
is gone.

First tests to write, in order of what they would have caught:

1. `playlists.to_form` / `to_rules` round-trip, against real rule blobs.
2. Rule validation: empty values, unknown fields, operators a field cannot
   take, negative limits.
3. `duplicates.resolve` refusing when a star cannot be migrated.
4. `workspace.key` and `.owner` marker handling — two users, one directory
   name.
5. Health checks against a fixture database.

### CI runs nothing

The workflow builds an arm64 image and pushes it. It does not run tests,
lint, or a type check. So the feedback loop is: push → four minutes of QEMU
build → pull on the Pi → hard-refresh → *the user* finds the bug. That loop
is how the duplicate-save bug was found — in production, after it had made
three playlists.

Adding a test job that runs before the build changes how every subsequent
change lands.

### Structure

- **`app/main.py` is 939 lines** — auth, jobs, settings, search, health,
  duplicates, staging, playlists and websockets in one module. Split into
  routers.
- **`app/static/app.js` is 1,215 lines in one global scope.** Six panels
  sharing globals. The tab switcher already broke once because a view name
  outlived its markup and took every panel down with it. The burger rewrite
  is a natural moment to split this per panel.
- **No linter or formatter config exists.**
- Two empty stray directories, `config;C` and `untagged;C`, left by a shell
  mishap. Untracked; just delete them.

---

## Operational backlog

Not code — things waiting in the library itself.

- **199 duplicate groups** and **15 staging items** sitting in the app.
- **ReplayGain at 56%** — run `beet replaygain`, then set Navidrome's
  ReplayGain mode to Track.
- **~62 GB reclaimable** from `music_old`, `tagged_old`,
  `music_backup_2026-09-04`.
- **Re-download list** from the migration: `~/redownload.txt`,
  `~/recheck-these.txt`, and 30 broken `.m4a`.
- **Kelly should sign in once** so her workspace is created and can be
  checked.
- **Playlist field vocabulary is unverified.** The rule fields offered by the
  editor are Navidrome 0.58's documented criteria plus the nine proven by
  existing playlists. `bpm` and `compilation` are the least certain. A
  rejected field surfaces Navidrome's own error naming it, so nothing fails
  silently — but it has not been confirmed end to end.

---

## Decisions log

Why things are the way they are, so they do not get re-litigated.

- **2026-09-06 — Last.fm backfill is a one-time manual import.** It has the
  history snapshots cannot reconstruct; snapshots have the accuracy and the
  coverage of both users. Neither replaces the other.
- **2026-09-06 — Burger navigation, both sizes.** One layout beats two, and
  six tabs is already the ceiling on a phone.
- **2026-09-06 — Health shows only what can be acted on.** Too many checks
  made it hard to see what mattered.
- **2026-09-06 — Smart playlists only, no track editing.** Navidrome edits
  ordinary playlists well; duplicating it would be a worse copy.
- **2026-09-06 — Playlist rules translate server-side.** Navidrome's nested
  operator shape never reaches the browser; unsupported rules are shown and
  marked uneditable rather than flattened and silently altered.
- **2026-09-05 — Everything is per-user, derived from Navidrome.** Replaced
  configuration that treated per-user state as a property of the files.
- **2026-09-04 — Track identity is `navidrome_uuid`.** Survives re-tagging
  and moves; a single-field PID would hash to the same value for every file
  missing the tag.

---

## Open questions

- Should the downloader stay a first-class tab, or fold into a smaller
  "add music" action once maintenance tools grow? (Leaning: stays.)
- Metadata editing writes to files. Does that run as a job with progress,
  like downloads, or synchronously with a spinner?
- Track deletion: quarantine directory per library, or one shared with
  `duplicates-removed/`?
- Does Kelly need any of the maintenance tooling, or is her account
  effectively read-only plus downloads?
