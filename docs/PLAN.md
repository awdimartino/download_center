# Navidrome Companion — what it is and where it is going

Working document. The repository copy is the source of truth; if something
here disagrees with the code, the code is what shipped and this is what we
meant. How things work today is in [FEATURES.md](FEATURES.md); known
defects are in [CODE_REVIEW.md](CODE_REVIEW.md).

Last updated: 2026-10-04.

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
audit, ingest, metadata editing, MusicBrainz matching, covers, ReplayGain.

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
3. **Nothing is deleted.** Files are moved to a quarantine directory, with a
   record of where they came from.
4. **Original code.** No lifting from SpotTube or similar.
5. **Identity is the UUID.** `navidrome_uuid` on the file survives re-tags,
   moves and a rebuilt database. Anything that needs to refer to a track
   across time refers to it by UUID.

---

## Now

The 2026-09-27 feedback round is finished apart from the code review, which
has been done and written up; fixing what it found comes next.

- [x] **Session 11 — code review** of everything. Done 2026-10-04; findings
      in [CODE_REVIEW.md](CODE_REVIEW.md), ordered by severity, **not yet
      fixed**. Work through it top-down before new features.
- [x] **Fix the review's critical and high findings.** Done 2026-10-04,
      one commit per finding (H6 fell out of C4's fix); each is ticked in
      CODE_REVIEW.md with what changed and what is left.
- [x] **Fix the review's medium findings.** Done 2026-10-04, one commit
      each. M29 is only partly done (a lock on `take()`; readers still
      share state.db's connection) and stays open in CODE_REVIEW.md.
- [ ] **Deploy and check on the Pi**: the state.db migrations
      (`play_collection`, `duplicate_dismissed.decided_by`), the new caches'
      cost, and Health's stamped-but-not-scanned row clearing (H6).
- [ ] **The review's low findings**, then readability.

### Next

- [ ] **Finish the rename: retire the Download Center leftovers.** Session 9
      renamed what users see (repo, image, service, UI, logs) and kept the
      rest so the Pi would not need its configuration touched. Rename:
      - **The `DC_` environment variables** (`DC_NAVIDROME_URL`,
        `DC_SPOTIFY_CLIENT_ID`, `DC_CONFIG_DIR`, … — the map in
        `config.py`, `Dockerfile`, `docker-compose.yml`, `.env.example`)
        to a new prefix such as `NC_`. Read the old `DC_` names as a
        fallback for a release, logging a deprecation warning, so an
        existing `.env` keeps working while it is updated.
      - **The Pi's config folder**, `~/Docker/download-center/config` →
        `~/Docker/navidrome-companion/config`. Stop the container, move
        the folder, update the bind mount; back up `state.db` first.
      - **The session cookie** `dc_session` (`auth.py:33`). Renaming it
        signs everyone out once, which a deploy does anyway.
      - **The container user** `downloader` (Dockerfile, entrypoint), and
        the `config.py` default `output_dir = ROOT / "untagged"` and
        compose's `STAGING_DIR` naming, both from the staging era.
      - **The local checkout folder** `download_center` (cosmetic; memory
        notes and the IDE workspace point at it).
      Then update SETUP.md (settings table, "Moving from the old name") and
      the 2026-10-04 decisions-log entry that says the prefix stays.
- [ ] **Split `app/main.py` into routers.** 2,287 lines holding every
      route. The review proposes a split.
- [ ] **Folders holding more than one album.** Found 2026-09-28: 57 of
      Alex's folders carry more than one album UUID (e.g. `Aiden
      Williams/Believe` holds *Believe*, *Breakup* and *Continuum EP*;
      several `Artist/Unknown Album` folders hold two). Kelly's library was
      not checked. The Library treats a folder as one album, so **Save
      album**, **Find matches → Use this**, **Combine** and ReplayGain treat
      such a row as one record and merge or mis-measure it. Likely cause,
      unverified: `unfuse.py` split fused albums by tag without moving
      files. Plan: (1) a guard - flag those rows and refuse folder-wide
      actions on them (**done**, CODE_REVIEW H9); (2) a throwaway script re-filing those tracks by
      their own tags, with the move list reviewed before it runs. UUIDs do
      not change, so stars and plays are unaffected.
- [ ] **Genre merge and rename.** Split out of Session 8, which shipped the
      tally alone. Reuse the album editor's merge-search pattern: a debounced
      search across the genre tally, picking a target folds the source
      genre's tracks into it. Needs a write path first - `genre` is not in
      `filer._EASY`, so `write_tags` silently drops it today; every track
      carrying the source genre has to be retagged and rescanned.
- [ ] **MusicBrainz seeding from Spotify data.** A "seed a release" button
      that opens MusicBrainz's add-release form pre-filled, using its
      existing seeding format. Only if the genre and ReplayGain cleanup shows
      enough releases need it.

---

## Later

### Music recommendations

Added 2026-10-04, not designed yet. Suggest music that isn't in the
library yet, seeded from what each person actually plays, with a way to send
a suggestion straight to Browse to download.

**Not Spotify.** `GET /v1/recommendations`, along with related artists and
audio features, returns 403 for any app created after 2024-11-27, and
Development Mode lost more in February 2026. Search alone gives only a rough
substitute.

**Likely sources**, both keyed on the MusicBrainz ids the library already
carries: Last.fm's `track.getSimilar` / `artist.getSimilar` (an API key
only), and ListenBrainz's similar-artist and recommendation data. Seeds come
from our own play history (`play_snapshot` + `play_imported`) rather than an
external account, so it works the same for Kelly. Filter out anything the
library already holds.

### Wrapped-style stats

Year figures, sessions and the hour-of-day chart are built. What is missing
is the *shape* of a year — discoveries (first plays), streaks, a month-by-
month story — which the five-minute timestamps now make possible.

### The candidate picker, beyond beets

*Find matches* shows beets' candidates and applies one. Still open: whether
Picard would match better than beets for the releases beets ranks badly.

---

## Done

The feedback round, in brief. Detail is in FEATURES.md and git history.

- **Sessions 1–8** (2026-09-28): smart playlists scoped to the owner's
  libraries; Library status bar, review filter and ReplayGain; the Drop
  page; Queue merged into Browse; Listening merged into Home; top albums,
  genres, hourly chart and sessions; inline track editing and the album
  merge search; quarantine from Library; the genre tally.
- **Library redesign and Combine** (2026-10-03): Albums / Artists / Needs
  attention, cover grid, side panel, select mode, combine into album,
  squared covers.
- **Session 9 — rename and setup docs** (2026-10-04): Download Center is
  now **Navidrome Companion** — repository, image
  (`ghcr.io/awdimartino/navidrome-companion`), compose service, UI and
  logs; the Pi redeployed under the new name. The docs were rewritten as
  SETUP.md and FEATURES.md; HANDOFF.md, FIXES.md and ARCHITECTURE.md were
  folded into them and removed.
- **Session 10** — `app.js` split into sixteen ES modules, no bundler.
- **Sessions 12–14 — aesthetics** (2026-10-04): the Library and Browse
  redesigns, the Home cover, fade-ins, one style for every dropdown, and
  Home's statistics made fast (`memo.py`).
- Earlier: play-count collection (2026-09-06, five-minute readings since
  2026-09-26), the Last.fm backfill (41,203 plays), the Health cut-down,
  burger navigation, the direct-to-library redesign that removed staging and
  the download ledger, tests and a CI gate.

---

## Operational backlog

Not code — things waiting in the library itself. Numbers are from late
September 2026; verify before acting.

- **~296 duplicate groups to review.** About 50 are a single beside its own
  album rather than true duplicates. Do not bulk-resolve while music is
  being filed.
- **ReplayGain** is incomplete; *Measure all* in Library's Needs attention.
- **~62 GB reclaimable** from `music_old`, `tagged_old`,
  `music_backup_2026-09-04` on the Pi.
- **30 broken `.m4a`** from the migration (`tools/fix_broken_m4a.py`).
- **5,572 imported plays are still day-granular**, because those tracks'
  artist tags changed since the import; `python -m app.lastfm <user>
  --times` after a tag cleanup picks up more.
- **Playlist field vocabulary is unverified** end to end. `bpm` and
  `compilation` are the least certain. A rejected field surfaces Navidrome's
  own error naming it, so nothing fails silently.

---

## Decisions log

Why things are the way they are, so they do not get re-litigated.

- **2026-10-04 — Renamed to Navidrome Companion.** It stopped being only a
  downloader long ago. The `DC_` environment prefix and the Pi's config
  folder stayed for now, so the deploy touched no configuration; retiring
  them is on the Next list.
- **2026-10-04 — Two user-facing docs.** SETUP.md for installing, FEATURES.md
  for what everything does and how. The handoff document went stale within
  weeks of being written; the code and these two are what stay true.
- **2026-09-26 — Play counts read every five minutes, not nightly.** At that
  cadence a rise is almost always one play, and Navidrome's `play_date`
  gives its exact time — which is what makes sessions and hour-of-day
  possible.
- **2026-09-25 — The download ledger is gone.** It made a track that left
  the library permanently unfetchable. Browse asks Navidrome what is held
  instead, and nothing refuses a download.
- **2026-09-25 — One road into the library, no gate.** beets refused 82% of
  what it was given for mechanical reasons. Everything is filed at once by
  its own tags; matching is a manual tool in Library.
- **2026-09-06 — Last.fm backfill is a one-time manual import.** It has the
  history snapshots cannot reconstruct; snapshots have the accuracy and the
  coverage of both users. Neither replaces the other.
- **2026-09-06 — Burger navigation, both sizes.** One layout beats two, and
  six tabs is already the ceiling on a phone.
- **2026-09-06 — Health shows only what can be acted on.**
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

- Does Kelly need any of the maintenance tooling, or is her account
  effectively read-only plus downloads?
- Should drops and uploads trigger a Navidrome scan the way downloads do?
  (They do not today; see CODE_REVIEW.md.)
