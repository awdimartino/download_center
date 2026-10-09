# Navidrome Companion — what it is and where it is going

Working document. The repository copy is the source of truth; if something
here disagrees with the code, the code is what shipped and this is what we
meant. How things work today is in [FEATURES.md](FEATURES.md); known
defects are in [CODE_REVIEW.md](CODE_REVIEW.md).

Last updated: 2026-10-09.

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

**Where things stand (2026-10-09, end of day).** Everything below marked
done is deployed: the Pi runs image `fd87b51`, healthy. The day's work, in
order: the Download tab and its recommendations; the rename's leftovers
(`NC_`, `companion`, the config folder); folders holding several albums
separated; SoundCloud and Bandcamp fallbacks, quality-first choice, and
deno for YouTube; listening history relinked to re-identified songs;
Missing tracks in the album panel; the Quarantine page; and
`duplicates-removed/` renamed `quarantine/`. Each is a checked item in
Next, and in FEATURES.md.

**Later the same evening**, deployed: the Pi runs `2b3ddee`, healthy. The
`DC_` fallback dropped, singles offered to combine only when they name one
record, Merge from Missing tracks, and the quarantine's old name retired.

Open, in the order worth doing: the browser half of the Pi checks below;
Alex's by-hand list (Operational backlog); Genre merge and rename; then
Later.

Nobody but Alex uses this, so breaking changes are fine: no fallbacks for
old names are needed beyond migrating the Pi's own data.

- [x] **Session 11 — code review** of everything. Done 2026-10-04; findings
      in [CODE_REVIEW.md](CODE_REVIEW.md), ordered by severity, all since
      fixed.
- [x] **Fix the review's critical and high findings.** Done 2026-10-04,
      one commit per finding (H6 fell out of C4's fix); each is ticked in
      CODE_REVIEW.md with what changed and what is left.
- [x] **Fix the review's medium findings.** Done 2026-10-04, one commit
      each. M29 is only partly done (a lock on `take()`; readers still
      share state.db's connection) and stays open in CODE_REVIEW.md.
- [x] **Fix the review's low findings.** Done 2026-10-04, one commit
      each. L19 was decided: a source at or below 192 kbps is encoded at
      192. L37 had already been fixed by M27 and only gained a test.
- [ ] **Check on the Pi.** Server side done 2026-10-09 against image
      8948bb3: both state.db migrations are in (`play_collection`,
      `duplicate_dismissed.decided_by`); Home's statistics cost 0.31s cold
      and 0.03s cached; `tools/` is in the image (L34);
      `config/.cover-survey.json` survived the restart (L21); a direct
      YouTube link passes the network guard and a LAN address is refused
      (L42); a cross-site POST gets 403 and a same-origin one reaches the
      session check (L38); a real YouTube download landed at 192 kbps
      (L19). H6 checked 2026-10-09 against `2b3ddee`: every library's
      stamped files on disk match Navidrome's index (6,851, 472 and 112),
      so *Stamped but not yet scanned* is rightly absent. **Still to
      check,** in a browser: downloads and the
      socket over the LAN address, a save in Settings, and the cookie
      being re-sent after a day (L33). Also first real uses, never yet
      clicked on the Pi: Missing tracks (a Download), the Quarantine page
      (a Restore, then the star coming back in Navidrome), and a download
      that falls back to SoundCloud or Bandcamp.
- [x] **The review's readability items.** Done; every item in
      CODE_REVIEW.md is ticked (168 of 168) as of 2026-10-08.

### Next

- [x] **Finish the rename: retire the Download Center leftovers.** Done
      and deployed 2026-10-09. Environment variables are `NC_…` (the `DC_` names are
      still read, with a warning naming each, and the shipped compose
      falls back to them), `STAGING_DIR` is `WORKSPACE_DIR`, the session
      cookie is `nc_session` (everyone signed out once), the container user
      is `companion` (same uid), the source default workspace is
      `workspace/`. **Left as it is:** the local
      checkout folder `download_center`. Renaming it would orphan the
      assistant's memory, which is keyed on the folder's path, and the
      IDE workspace, for nothing anyone sees.
      - [x] **Deployed, and the Pi's config folder moved** to
        `~/Docker/navidrome-companion/config` (2026-10-09, image 8948bb3).
        The compose file's `DC_LASTFM_*` lines are `NC_` now, so nothing
        on the Pi uses an old name. Backups: `docker-compose.yml.bak-
        20261009-nc-rename` and `config/state.db.bak-20261009-nc-rename`.
      - [x] **Drop the `DC_` fallback.** Done 2026-10-09: nothing on the Pi used a `DC_`
        name, and nobody else runs this, so it went:
        `config.environ`'s old-prefix branch, compose's `${DC_…}` and
        `${STAGING_DIR}` defaults, SETUP.md's mentions.
- [x] **Split `app/main.py` into routers.** 2,287 lines holding every
      route. The review proposes a split. **Done** (CODE_REVIEW R1):
      `app/main.py` is the application, its middleware and the page;
      routes are in `app/api/`, one router per panel; job state in
      `app/jobs.py`, the socket broker in `app/events.py`, the loops in
      `app/background.py`.
- [x] **Folders holding more than one album.** Done 2026-10-09 with
      `python -m app.separate`: in each folder the album it is named for
      stays and every other album's tracks are filed where their tags say,
      through `filer.file_track`, so they join any copy already there and
      keep their track and album UUIDs. 66 files from 38 of Alex's folders
      moved (32 folders mixing named albums, 6 `Artist/Unknown Album`
      folders holding a named album beside loose files); four folders named
      by an older sanitiser (`Kosu_/Daft_`) were emptied and pruned.
      Kelly's library had none. The Library's "N albums" flag now counts
      only files that name an album, as the guard does, so the 21 folders
      of untagged files are no longer flagged. **Left:** those untagged
      files, and one stray in `Valzugg/Afternoon`, need tagging, not
      moving. Plans are in the Pi's config folder as
      `separate-applied-1.json` and `separate-applied-2-1.json`.
- [x] **More places to download from.** Done 2026-10-09: SoundCloud and
      Bandcamp after YouTube Music, and the better-sounding copy when two
      sources are certain (FEATURES.md §5, `app/sources.py`). Found while
      checking the Pi: a correct YouTube match failed with a 403.
- [x] **Give yt-dlp a JavaScript runtime.** Done 2026-10-09: the image
      carries deno (from `denoland/deno:bin`, which Dependabot moves), and
      yt-dlp is installed with its `default` and `curl-cffi` extras for the
      EJS challenge solver and browser impersonation. Health shows a
      *JavaScript for YouTube* row. Keeping yt-dlp current is Dependabot's
      weekly pull request; Health warns past 60 days.
- [x] **The files Navidrome can no longer find.** Checked 2026-10-09: 814
      rows. 154 are quarantined duplicates (kept on purpose), 197 are gone
      with nothing like them left, and 460 are songs still in the library
      under a newer track UUID, mostly from the late-September stamping and
      refiling. Their listening history is credited to the live copies by
      `python -m app.relink` (aliases in `state.db`; nothing rewritten).
      **Left for Alex, by hand:** 10 stars and 6 ratings, on 11 songs, that
      only the old rows carry; and Navidrome's own play counts on the old
      rows (174), which its API can only add to as new plays. Then the old
      rows can be cleared from Navidrome's Missing Files page - except the
      quarantined ones, until the duplicates are settled.
- [x] **Missing tracks in the album panel.** Done 2026-10-09: the
      MusicBrainz release the files carry (Spotify otherwise), an edition
      switcher, and Download per track or for all of them, tagged into
      this album (FEATURES.md §8, `app/albumcheck.py`, `app/musicbrainz.py`).
- [x] **Quarantine as a general "remove".** Done 2026-10-09: a
      Quarantine page (FEATURES.md §9a) listing everything set aside, with
      Restore (to where it was, stars and plays included) and Delete for
      good; and Quarantine from the Library's selection bar and song
      search results. Duplicates links to it instead of its own read-only
      list. The folder itself was renamed `duplicates-removed/` ->
      `quarantine/` the same day, on start-up, ledger paths following
      (305 old rows point at files that were gone before the rename and
      were left as history).
      - [x] **Retired the old name.** Done 2026-10-09: the 153 files buried
        in `/music/quarantine/duplicates-removed/duplicates-removed/` were
        lifted to where `quarantine.original_path` put them, their ledger
        rows following, and the old folders removed from `/music` and
        `/test` (backup: `config/state.db.bak-20261009-flatten`). The 306
        rows still naming the old folder have no file behind them. Then
        `walk.OLD_QUARANTINE_NAMES`, `rename_old_folders` and its start-up
        call went.
- [x] **Singles that belong together, fixed.** Done and deployed
      2026-10-09: every one-track folder by an artist was offered as one
      album - 311 groups on the Pi, three real. Now they must name the same
      record (`library.record_key`); FEATURES.md §8.
- [x] **Merge from Missing tracks.** Done and deployed 2026-10-09:
      a track in another album offers Merge…, opening the combine dialog
      with this album and that copy (FEATURES.md §8).
- [ ] **Genre merge and rename.** Split out of Session 8, which shipped the
      tally alone. Reuse the album editor's merge-search pattern: a debounced
      search across the genre tally, picking a target folds the source
      genre's tracks into it. Needs a write path first - `genre` is not in
      `filer._EASY`, so `write_tags` silently drops it today; every track
      carrying the source genre has to be retagged and rescanned.

---

## Later

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
- **Recommendations and the Download tab** (2026-10-08): Browse renamed
  **Download** and its rail **Queue**; five shelves while nothing is
  searched (new releases and missing albums from your favourite artists,
  similar artists, songs like what is on repeat, top albums in your
  genres). Favourites are a year of plays plus stars and ratings; "similar"
  is Last.fm's, because Spotify's recommendation endpoints are closed to
  new apps. Computed in the background once a day per person, with "not
  interested" per card or per artist. See FEATURES.md §5.
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
September 2026 unless dated; verify before acting.

- **11 songs need a star or rating put back by hand** (2026-10-09): the
  stars sit only on Navidrome rows for files it can no longer find.
  Ambrosia - Art Beware (★, 4) and How Much I Feel (★, 4), *Life Beyond
  L.A.*; Geese - Au pays du cocaine (★); Mineral - Dolorosa (★) and
  Gloria (★); Player - Bad News Travels Fast (3); Queens of the Stone Age
  - Another Love Song (★); Sora - revans (★, 4); The Alan Parsons
  Project - Children of the Moon (★), Eye in the Sky (★, 4), Silence and
  I (★, 4). Then clear Navidrome's Missing Files page, keeping rows for
  quarantined files.
- **The quarantine holds 666 tracks, 5.2 GB** (2026-10-09): 530 lost to
  duplicates, 136 removed by hand. The Quarantine page restores or
  deletes them; *Delete everything older than* frees the space once the
  duplicate decisions are trusted.
- **21 folders of untagged files**, mostly `Artist/Unknown Album`, plus a
  stray in `Valzugg/Afternoon`, need album tags (Edit details), not moving
  (2026-10-09).

- **~296 duplicate groups to review.** About 50 are a single beside its own
  album rather than true duplicates. Do not bulk-resolve while music is
  being filed.
- **ReplayGain** is incomplete; *Measure all* in Library's Needs attention.
- **~62 GB reclaimable** from `music_old`, `tagged_old`,
  `music_backup_2026-09-04` on the Pi.
- **30 broken `.m4a`** from the migration (`tools/fix_broken_m4a.py`).
- **5,572 imported plays are still day-granular**, because those tracks'
  artist tags changed since the import; `python -m app.lastfm <user>
  --times` after a tag cleanup picks up more. Rerun 2026-10-09 without
  one: 49,983 scrobbles fetched, 0 rows resolvable, 5,565 with no scrobble
  that resolves to the track that day. It needs the tag cleanup first.
- **Playlist field vocabulary** checked against Navidrome's source
  (`model/criteria/fields.go`, master, 2026-10-09): every field the editor
  offers is accepted, `bpm` and `compilation` included; `genre` is a tag
  and `artist` / `albumartist` are roles, added to the map at start-up. Not
  yet a saved round trip against the Pi's own version. A rejected field
  surfaces Navidrome's own error naming it, so nothing fails silently.

---

## Decisions log

Why things are the way they are, so they do not get re-litigated.

- **2026-10-09 — The rest of the rename, with a fallback.** `NC_` replaced
  `DC_`, but the old names are still read for a release, because an `.env`
  nobody has looked at in months should not silently stop configuring the
  app. The checkout folder keeps its name; see the Next list The fallback was dropped the same
  day, once the Pi's compose file was renamed: nobody else runs this.
- **2026-10-09 — No MusicBrainz seeding.** Dropped from the plan: a "seed a
  release" button was only ever conditional on the cleanup showing a need,
  and it was not wanted.
- **2026-10-08 — Recommendations from Last.fm, computed daily.** Spotify
  closed recommendations and related artists to new apps; Last.fm needs an
  API key and nothing else. A pass is about a hundred requests, so it runs
  in the background and is stored, and what changes between passes (what is
  held, what was dismissed) is applied on every read.
- **2026-10-04 — Renamed to Navidrome Companion.** It stopped being only a
  downloader long ago. The `DC_` environment prefix and the Pi's config
  folder stayed for that deploy, so it touched no configuration.
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
