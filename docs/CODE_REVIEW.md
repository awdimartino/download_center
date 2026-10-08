# Code review — 2026-10-04

A read-only review of the whole codebase (Session 11), at commit `6e45ae2`.
A second round, at `f900521`, follows the first and is numbered `2H`, `2M`,
`2L` and `2D`; see [Round 2](#round-2--2026-10-04).
Fixes are being worked through top-down; a ticked item carries a **Fixed:**
note saying what changed. Findings are ordered by severity, then
by how much they matter in daily use. Tick them off as they land; do not
delete them — a closed item is the record that it was looked at.

How it was done: four reviewers covered ingest and downloads; the Library;
listening history and Home; and the platform (auth, config, Health,
Duplicates, Playlists, CI). Each was given the surprises from an earlier
feature survey to confirm or refute, then looked for more. Findings marked
**verified** were reproduced, either by running code or by checking the
Pi. Those marked **unverified** are from reading the code, and the
uncertainty is stated.

Severity:

- **Critical** — damages the library, its identity, or what lands in it,
  silently, in ordinary use.
- **High** — wrong results or a broken feature in ordinary use, or a
  privacy leak between accounts.
- **Medium** — wrong in some cases, or a robustness or performance problem
  with a visible cost.
- **Low** — edge cases, hardening, small UX.
- **Readability** — no behaviour change: stale text, dead code, structure.

The recurring pattern is the one the old fix list named: **silent
failure.** Prefer fixes that make a failure visible over ones that only make
it less likely.

---

## Critical

- [x] **C1. *Use this* lets beets move files, because the Pi's beets
      configs say `move: yes`.** *Verified on the Pi.*
      `app/beets_match.py` (`apply_choice`), `app/beets_runner.py:156-181`.
      All three workspace configs (`alex`, `kelly-2`, `test-5`) predate the
      `move: no` template and still say `move: yes`; `ensure_config` never
      overwrites a config, and `apply_choice` overrides only `singletons`
      and `search_ids`. So applying a MusicBrainz match lets beets move the
      files into *its* path layout. `filer.audio_in(path)` then finds nothing
      at the old folder, `after_retag` re-points nothing, the filer moves
      nothing, and the operation reports success. The album ends up where
      beets put it, with its registry entry under the old key.
      **Until fixed, do not press *Use this*.**
      *Fix:* set `config["import"]["move"] = False`, `copy = False`,
      `write = True` inside `apply_choice` regardless of the file; and edit
      the three configs on the Pi.
      **Fixed:** `apply_choice` now forces all three; reproduced first with a
      `move: yes` config, and a test runs the same probe. The Pi's configs
      are no longer dangerous but still say `move: yes`.

- [x] **C2. The featured-artist regex deletes the end of real titles and
      artists, so some songs can never match and others match the wrong
      recording.** *Verified.* `app/matcher.py:48` (`_FEAT`). There is no
      word boundary before `feat|ft|featuring|with`, and the brackets are
      optional, so everything from any "with " or "…ft " onward is removed:
      "With or Without You" → `""`, "Dancing with Myself" → `dancing`,
      "Daft Punk" → `da`, "Gift of Love" → `gi`. The first can never pass
      the title gate. "Dancing with Myself" scores 0.99 against "Dancing with
      the Stars Theme" by the same artist and length, so the wrong file is
      downloaded and filed.
      *Fix:* only strip a bracketed `(feat./ft./featuring/with …)` or a bare
      `\b(feat|ft|featuring)\b\.?\s.*`; never a bare "with". Add the examples
      above as tests.
      **Fixed:** one `matcher.FEATURING`, now also used by `lastfm.normalise`
      (an identical copy of the bug) and `duplicates.normalise` (a variant:
      "Gift of Love" and "Gift Horse" both became `gi` and could be grouped
      as copies). The right song now outscores the wrong one (0.99 vs 0.90)
      instead of tying, but the wrong one alone would still clear the 0.70
      floor: `token_set_ratio` gives a title that shares most of its words 0.77.
      Imported Last.fm plays matched under the old regex were not re-checked.

- [x] **C3. Match candidates can be shown under the wrong album, and *Use
      this* then applies one album's release to another, fusing them.**
      `app/static/js/library.js:961-996`, `app/main.py:1775`,
      `app/operations.py:113-115`. `askForCandidates` remembers the album
      it asked about, then starts the `candidates` operation; if one is
      already running (yours or anyone's) the server returns that one. Its
      result is drawn as "Matches for *the album you asked about now*", and
      each *Use this* posts the current album's folder with the earlier
      album's release id. The current album is retagged as the other
      release; `after_retag` finds that key already registered, so its files
      adopt the other album's UUID and move into its folder.
      *Fix:* carry `library_id` and `folder` in the operation and its result;
      drop any result whose folder is not the one asked about. See also H3.
      **Fixed:** operations carry a `target`; the candidate result names its
      library and folder; the page drops answers for another album and says
      so when a lookup is refused because another is in flight. The server
      also remembers which releases were offered per (user, library, folder)
      and refuses to apply any other (409), so no client race can fuse two
      albums. Reproduced first: the old route applied album A's release to
      album B. The refusal message still shows another person's operation to
      the browser; that is H3.

- [x] **C4. `survey` walks the quarantine, and `unfuse --apply` /
      `backfill --apply` act on what it finds.** `app/survey.py:134`,
      feeding `app/unfuse.py:459` and `app/backfill.py:111`.
      `duplicates-removed/` sits inside each library root. An album with 10
      live files on UUID A and 12 quarantined copies on an older UUID B is
      classed "split"; `_majority` picks B and `unfuse --apply` retags the 10
      live files to B, changing the album's Navidrome identity and losing
      album-level stars and plays. It also writes tags onto quarantined
      files. `backfill` can record a quarantined copy's UUID in the
      registry.
      *Fix:* one shared library walker that skips `duplicates-removed/` and
      any directory with an empty `.ndignore`, used by survey, diskaudit and
      `tools/fingerprint.py`. Do not run `unfuse --apply` until then.
      **Fixed:** `app/walk.py` (`library_files`) skips `duplicates-removed/`
      at the root and any directory with an empty `.ndignore`; `survey` (so
      `unfuse` and `backfill`) and `diskaudit` use it, and
      `tools/fingerprint.py` carries a copy since it runs without the app.
      Reproduced first: the old plan retagged all ten live tracks to B and
      retired A. `tools/fix_broken_m4a.py` still walks everything.

---

## High

- [x] **H1. A MusicBrainz match reports success, and marks the album
      reviewed, even when beets applied nothing.** `app/beets_match.py:152-171`,
      `app/beets_runner.py:269-289`, `app/main.py:1819-1822`. When the
      chosen release is not among beets' candidates, `apply_choice` prints
      `{"applied": false}` and exits 0; `import_chosen` never reads stdout
      and reports `imported: 1`. The UI says "Retagged…" and the album leaves
      the review queue for good. The client's "That release did not tag it"
      branch can never fire.
      *Fix:* parse the last JSON line, as `candidates()` already does, and
      only mark reviewed when `applied` is true.
      **Fixed:** `import_chosen` reads beets_match's answer (shared
      `_answer` with `candidates`); `applied: false` is `skipped: 1`, an
      unreadable answer is a failure. The route already marked reviewed only
      on `imported`. Reproduced first with the subprocess faked.

- [x] **H2. Applying a match stamps the new album UUID onto files beets did
      not retag.** `app/filer.py:361-406`, `app/main.py:1830-1833`.
      `after_retag` takes the new key from the first file only and writes the
      settled UUID onto every file in the folder. An extra file beets left
      unmatched, or a download that landed during the minutes-long
      operation, keeps its old tags but gets the new UUID — and the old key
      is re-registered against it. Two names, two folders, one album UUID:
      the "shared album UUID" bug class again.
      *Fix:* after applying, group files by their actual key; re-point only
      the group that changed; refuse if the keys disagree.
      **Fixed:** `after_retag` reads every file's key and raises
      `NotEditable` if they disagree, before touching the registry or any
      UUID; a file with no album tag is left out of the album. *Use this*
      then fails with that message and moves nothing, and now marks the
      album reviewed last (part of M13). Files beets did retag stay in the
      old folder with new tags and the old UUID, for a person to sort out.
      Reproduced first.

- [x] **H3. Long operations are global by name: one person's result goes to
      another, and every person's results are readable by all.**
      `app/operations.py:75-118`, `app/main.py:2085-2090`, `app/static/js/main.js:157-163`.
      Operations are keyed `candidates`, `import`, `combine`, `replaygain`,
      `audit`. If Kelly starts a combine while Alex's runs, she gets
      `started: false` and Alex's operation; her combine is silently never
      run, and the result is pushed only to Alex. `GET /api/operations`
      returns everyone's operations, including results with folder names,
      paths and candidate lists; only the browser filters. Contradicts "per
      user, strictly private".
      *Fix:* key operations by `(user, name)` — by target for match and
      apply — and filter `/api/operations` on the server.
      **Fixed:** keyed by `(owner, name)`; `get`, `report`, `stop` and
      `stopping` all take the owner, `/api/operations` returns only the
      caller's, and the ReplayGain stop can only name your own run. Not keyed
      by target: C3's `target` and offered-release check already cover
      match and apply. The old suite had a test asserting the bug (Kelly's
      start returned Alex's operation). Trade-off: two people can now run
      ReplayGain, or a lookup, at the same time on the Pi.

- [x] **H4. Listening overcounts, by default.** `app/playcounts.py:597`
      (`started = opening.get(key, 0)`). For a range that starts before a
      track's first stored reading — including the default **All time** —
      there is no opening value, so the track's whole lifetime count at its
      first reading is counted as plays in range, on top of the imported
      Last.fm plays that lifetime count already includes. Measured: lifetime
      100 at the first reading, 90 imported, 2 new → shows 192, should be 92.
      *Fix:* fall back to the track's first stored reading, not 0 — or,
      better, compute Listening from the same per-play list Home uses
      (fixes H5 and M1 too).
      **Fixed** the narrow way: a missing opening balance falls back to the
      track's row from the first reading, else 0. "First reading" is
      recorded in a new one-row `play_collection` table by `take`, even when
      that reading stored nothing; an existing database backfills it from
      `MIN(taken_on)`. Keying it on the first *stored* reading instead broke
      six tests that model a fresh install, whose first real plays would
      have been read as a baseline. Reproduced first (192 → 92). The
      one-list rewrite is still open as M1.

- [x] **H5. Home undercounts: the first play of every newly played track is
      lost.** `app/overview.py:98-101`. Every track's first stored row is
      treated as a baseline and skipped. `take()` stores only counts above
      zero, so a track first played after collection began first appears at
      count 1, and that play is thrown away. Measured: a track played twice
      since collection began shows 1; played once, it is missing entirely.
      This hits exactly the music you are discovering.
      *Fix:* treat a row as a baseline only if it came from that user's very
      first reading; any later first row counts from 0.
      **Fixed** with H4's rule rather than per user: a row is a baseline only
      if it came from the reading when collection began (`play_collection`).
      Per user would read a new account's first plays as its baseline, since
      a user with no plays has no rows. Reproduced first; Home and Listening
      now give the same totals (92 / 2 / 1) for the same history. Two hourly
      and session tests had seeded a later first row as a baseline and were
      moved to the first reading.

- [x] **H6. Health raises a permanent false warning because the disk audit
      counts quarantined files.** `app/diskaudit.py:104`; effects at
      `app/health.py:132, 169, 371, 400`. *Stamped but not yet scanned* is
      `audit.stamped − indexed_stamped`, which equals the number of stamped
      quarantined files — a warning that counts toward the badge and that no
      scan can clear. *Tracks with no UUID*, *Duplicate UUIDs*, *Album UUIDs
      spread across directories* and *Audio files* are also inflated.
      *Fix:* the shared walker from C4.
      **Fixed** by C4's commit: `diskaudit.run` uses `walk.library_files`,
      and a test checks quarantined files are not counted. Not yet checked
      against the live Health page on the Pi.

- [x] **H7. A stray cover image at the inbox root is copied into every
      download's album folder.** `app/filer.py:586`, `app/inbox.py:133-134`.
      Downloads are moved to the inbox root and filed from there, so
      `_carry_cover(source.parent)` looks at the inbox root. Any
      `cover.jpg` / `folder.jpg` / `front.*` dropped there over SMB (and
      never cleaned up — see M17) is copied into every later downloaded
      album with no folder cover, and Navidrome prefers it to the correct
      embedded art. Loose files filed from the library root do the same with
      any image there.
      *Fix:* only carry a cover from a subfolder of the inbox, never the
      inbox or library root; or deliver each job into its own subfolder.
      **Fixed** the first way: `file_track` skips `_carry_cover` when the
      source's folder is the inbox root or the library root. Reproduced
      first for both. Albums that already received a stray cover on the Pi
      keep it; that needs finding by hand (a `cover.jpg` identical across
      unrelated albums).

- [x] **H8. Smart playlists: "in the last" shows a date picker and saves a
      date.** `app/static/js/playlists.js:80, 195`. The check is
      `operator.indexOf("InTheLast") >= 0`, case-sensitive; the operator is
      `inTheLast`, so only "not in the last" matches. Choosing "date added
      in the last" shows `<input type=date>` and saves "2026-09-01" as the
      day count. Opening an existing `inTheLast: 30` playlist puts "30" in a
      date input, which blanks it, and Save is then refused.
      *Fix:* compare case-insensitively against the operator names, and add
      a frontend test that every operator string in the JS exists in
      `playlists.OPERATORS`.
      **Fixed:** `playlists.js` names `DAY_OPERATORS` in full, and a
      frontend test checks them against `OPERATORS["date"]` and forbids
      substring matching on operators. The server also accepted any string
      for a date field; `_coerce` now requires a day count for
      `inTheLast`/`notInTheLast` and a `YYYY-MM-DD` date for `before`/`after`.
      Reproduced first in V8 (`"inTheLast".indexOf("InTheLast")` is -1).
      Not clicked through in a browser. A playlist already saved with a
      date as its day count opens with an empty box and Save refuses it
      until a number is entered.

- [x] **H9. A folder holding two albums is retagged as one, with no guard.**
      `app/filer.py:283-306, 343-358`, `app/main.py:1617-1632, 1814-1833`.
      `album_key_of` reads the first file only; *Save album*, *Use this*,
      *Combine* and ReplayGain treat the folder as one record. 57 such
      folders are known (PLAN.md), and new ones can arise when two names
      sanitise to one folder (`AC/DC` and `AC_DC`, case-insensitive
      filesystems, truncation). The second album is silently absorbed and
      its registry row orphaned. `library.py:342-346` already knows when a
      folder holds two Navidrome album ids.
      *Fix:* refuse folder-wide actions when the files' keys disagree, and
      flag those rows in the list.
      **Fixed:** `filer.require_one_album` (files naming an album must
      share one key; untagged strays don't count) guards `retag_album`
      (*Save album* → 422), *Find matches*, *Use this*, *Combine* and cover
      apply (409), and ReplayGain skips such folders. The list sends
      `albums_here` (Navidrome album ids in the folder) and the page flags
      it. Reproduced first: a rename absorbed the second album and orphaned
      its registry row. The flag counts Navidrome ids and the guard counts
      tag keys, so a split album (one name, two UUIDs) is flagged but not
      refused.

---

## Medium

### Listening history

- [x] **M1. Home and Listening cut days differently.** `app/playcounts.py:571-581`
      vs `app/overview.py:107`. Listening's `value_at` buckets by the UTC time
      of the *reading*; Home by the local time of the *play*. Measured in
      New York: a play at 20:58 local on 4 October is credited to 5 October
      in Listening, and "Today" stops moving after 8pm. Together with H4/H5,
      the two halves of one page disagree.
      *Fix:* one per-play list, filtered by local date, for both.
      **Fixed:** the per-play list moved to `playcounts.increments`; Home
      uses it as before and `plays_between` now sums it by local day instead
      of subtracting readings. The H4 opening-balance code went with the
      old method; H4's baseline rule lives on in the list. A count that
      falls then rises now counts the rise. The test helper `played()` had
      dated every play 1 March; it now leaves `play_date` unset unless asked.
      Reproduced first.

- [x] **M2. Re-running the Last.fm import after `--times` doubles plays; the
      hand-over day is imported twice.** `app/lastfm.py:240, 311-324, 606-607`.
      A plain `--apply` re-inserts bare-date rows beside the timestamped
      rows `--times` wrote. The "stop at the first snapshot" cutoff compares
      a bare date with a timestamp as text (`"2026-09-25" < "2026-09-25T…"`),
      uses the minimum across all users, and mixes local and UTC.
      *Fix:* refuse a plain import when timestamped rows exist; compare
      instants against this user's first reading.
      **Fixed:** `--apply` without `--times` exits, writing nothing, when the
      account already has timestamped rows. The cutoff is now an instant:
      when collection began (`playcounts.baseline_stamp`, H4's rule, so
      global rather than per user), compared with each scrobble's unix time.
      A bare-date first reading held the total at the end of its day, so its
      cutoff is the next local midnight; the old code dropped that day's
      scrobbles. Tests cover the cutoff and the count, not the exit itself.

- [x] **M3. Two files sharing a UUID can make phantom plays.**
      `app/playcounts.py:191-201`. `_current` keeps whichever annotation row
      comes last, in no fixed order; counts of 5 and 2 can alternate, and
      each rise back to 5 counts three plays.
      *Fix:* combine duplicates deterministically (max).
      **Fixed:** `_current` keeps the higher count. Reproduced first: with
      the 5 row first, the reading was 2.

- [x] **M4. "Plays in view" sums only the top 50 tracks.**
      `app/overview.py:235`. *Fix:* sum the full range.
      **Fixed:** summed from `plays_between` over the range. Reproduced
      first (3 for 6 with a limit of 1).

- [x] **M5. A slow Listening response can overwrite a newer one.**
      `app/static/js/listening.js:103-141`. Click Year, then All time (cached,
      fast): Year's answer arrives last and is drawn under "All time".
      *Fix:* a request counter or `AbortController`. Library search
      (`loadAlbums`) and its merge search have the same race.
      **Fixed:** a request counter in `loadListening`, `loadAlbums` (which
      also stops a stale "more" page being appended to a new search) and the
      merge search, which also drops an answer once the box is cleared.
      Checked in headless Chromium against a fixture server (not the Pi):
      a Year answer delayed by two seconds no longer replaces All time.

- [x] **M6. The Dates button does not look selected until a range is
      submitted** (reported by the user). `app/static/js/listening.js:162-175`.
      Clicking *Dates* only unhides the pickers and sets `aria-expanded`;
      the `active` class moves only in the form's submit handler (`:185-187`).
      While choosing, "All time" (or whatever was last) stays highlighted,
      and closing the pickers without submitting leaves no hint which range
      is shown. (Confirmed by the user as the bug they meant.) Related: the default range is written in three
      places (`index.html:155`, `listening.js:26`, `overview.py:249`), and
      the selection is a class only, invisible to screen readers.
      *Fix:* a `markRange()` helper that sets the class and `aria-pressed`,
      called on load, on a range click, and when Dates opens.
      **Fixed:** `markRange`/`markShown` set the class and `aria-pressed`;
      opening Dates marks Dates, closing it unsubmitted marks the range
      still shown. The markup no longer carries a default, and a frontend
      test ties `listeningDays` to `overview.OPENING_DAYS`. Checked in
      headless Chromium: Dates is the one highlighted while open, and All
      time again after closing.

### Library

- [x] **M7. `album_dir`'s depth check can be bypassed, and is too strict for
      legitimate folders.** `app/library.py:727-738`. Parts are counted before
      resolving, so `Artist/.` passes as two parts and resolves to the
      artist directory; `retag_album` then uses `rglob` and would merge the
      whole discography (needs a hand-made request). Meanwhile real albums at
      depth 1 or 3 (`Album/CD1`) can be neither edited nor matched, yet
      `without_gain` still lists them, so *Measure all* skips them every run
      and the No ReplayGain count never reaches zero. A folder under
      `duplicates-removed/` also passes, so a crafted edit could re-file
      quarantined files.
      *Fix:* reject `.`/`..` and the quarantine; count parts after resolving;
      make `without_gain` agree with `album_dir`.
      **Fixed:** `.`/`..` parts are refused, depth is counted after
      resolving, and `album_dir` and `track_path` refuse the quarantine.
      Rather than narrowing `without_gain`, ReplayGain passes
      `any_depth=True`, so depth-1 and depth-3 folders are measured and the
      count can reach zero; editing and matching stay at exactly
      `artist/album`. Reproduced first (`Artist/.` resolved to `Artist`).

- [x] **M8. Editing an album opened from a song search writes the track
      artist as the album artist.** `app/static/js/library.js:341-344, 1114-1135`.
      The stub album is built with `artist: song.artist`. Change only the
      title in *Edit details* and every file's album artist becomes, say,
      "A feat. B"; the album moves or merges. Clicking before the tracks load
      throws (`plural(undefined)`).
      *Fix:* have `/api/library/album` return the album-level names and fill
      the stub from them.
      **Fixed:** `library.tracks` returns the album's most common album
      artist and album; `loadTracks` puts them on a stub, and Edit details
      waits until the tracks have arrived. Checked in headless Chromium
      against a fixture: the editor opened from a song shows the album
      artist, not "Artist feat. Guest".

- [x] **M9. Changing a track's artist can move the file without the
      confirm, and the panel goes stale.** `app/static/js/library.js:1629`,
      `app/library.py:204`, `app/filer.py:327-334`. The confirm checks
      `!album.artist`, which falls back to the track artist and so is almost
      never empty; the server moves the file whenever it has no album-artist
      tag. The panel keeps showing it in this album; a later combine using
      the old path fails.
      *Fix:* return `has_albumartist` per track and `moved` from the edit.
      **Fixed** as suggested: the confirm checks the track's
      `has_albumartist`, and a track whose edit reports `moved` leaves the
      panel with a note saying where it went. Checked in headless Chromium
      against a fixture: the confirm appears, and the moved track leaves.

- [x] **M10. Every edit locks the album for two minutes, with a wrong
      message.** `app/inbox.py:191-210` used at `app/main.py:1617, 1663, 1805, 1893, 1972`.
      "Settled" means nothing modified in `inbox_quiet_seconds`, and every
      edit modifies files. Fix a title, then its track number: 409 "still
      arriving". The same after squaring a cover, before combining fresh
      edits; ReplayGain skips recently edited albums.
      *Fix:* guard against active downloads/drains into the folder, or
      exempt paths this app just wrote.
      **Fixed** the first way: `inbox.deliver` and `drain` record the album
      folder each track was filed into, and edits, covers, combine, match
      and ReplayGain wait only while that folder received a track within
      `inbox_quiet_seconds` (`inbox.receiving`). The app's own writes no
      longer lock anything. Trade-off: a file copied straight into a
      library folder by hand, not through the inbox, is not noticed.
      Found in the browser checks: a fresh album could not be edited.

- [x] **M11. Retags leave the old folder behind.** `app/main.py:1814-1835`,
      `app/filer.py:264-280, 414-445`. *Use this* never prunes; elsewhere
      `_carry_cover` *copies* `cover.jpg` (and beets' fetchart adds one), so
      the old folder is never empty and is never pruned. Every rename, match
      or combine leaves `OldArtist/OldAlbum/cover.jpg`.
      *Fix:* once no audio remains, remove carried covers and prune; call it
      from *Use this* too.
      **Fixed:** `filer.leave_folder`, used by album and track retags (so
      combine) and by *Use this*: once no audio is left, a cover is removed
      only if a byte-identical copy now sits where the tracks went, then the
      folder is pruned. Any other file keeps the folder. An older test said
      a folder holding cover.jpg must be kept; it now uses a rip log, since
      a cover with an identical copy has in effect moved. Reproduced first.

- [x] **M12. Combine: an album already called the target name stays in a
      non-canonical folder; renumbering ignores discs.** `app/combine.py:76-82, 106-118`.
      Joiners go to the canonical folder, giving two folders for one UUID. A
      two-disc album plus a single becomes disc 2 starting at track 11, with
      every total 21.
      *Fix:* re-file the kept album when its folder differs; set `disc_no=1`
      or renumber per disc.
      **Fixed:** the already-named album is re-filed (`file_track`) into the
      canonical folder and its old one left via `leave_folder`; renumbering
      writes disc 1/1 (new `disc_total` in `write_tags`). Reproduced first.

- [x] **M13. A retag that fails part-way leaves the album mixed.**
      `app/filer.py:300-306`, `app/main.py:1831-1833`. A `NotEditable` on file
      *k* leaves 0..k−1 retagged, unregistered and unmoved; a failure in
      *Use this* happens after the album was already marked reviewed.
      *Fix:* open every file before writing any; mark reviewed last.
      **Fixed:** `filer.check_writable` opens every file (and checks it is
      writable) before `retag_album` or *Use this* writes any; reviewed is
      marked last since H2. A disk filling mid-write can still interrupt.
      Reproduced first.

- [x] **M14. Nothing stops two changes hitting one folder at once.**
      *Unverified at runtime.* Rename, combine, cover and match run in
      threads with no per-folder lock; a ReplayGain run lasts hours. rsgain
      and mutagen can write one file together, or rsgain can lose a file
      mid-move. *Fix:* a per-`(library, folder)` lock on every mutating path.
      **Fixed:** `app/folderlock.py`, a non-blocking lock per folder that
      also covers its parents and children. Album and track edits, cover
      apply and quarantine answer 409 while it is held; *Use this* and
      *Combine* fail as operations with the message; ReplayGain holds each
      folder while rsgain runs and skips one that is busy. Downloads filing
      into a folder are not locked; M10's `receiving` and H2's check cover
      those. Still unverified at runtime on the Pi.

- [x] **M15. beets' own index goes stale after every apply.** *Consequence
      unverified.* `app/beets_match.py:137-170`. Items are added at
      pre-move paths and never updated; a second match on a moved album may
      merge the stale entries (`duplicate_action: merge`) and map them
      instead of the real files.
      *Fix:* apply against a throwaway per-run library database; nothing
      reads beets' index.
      **Fixed:** `apply_choice` builds its `Library` in a temporary
      directory, closed and removed after the run (not `:memory:`; beets
      backs a database up beside itself while migrating). The C1 probe test
      now also checks the index is not the workspace's.

### Getting music in

- [x] **M16. One unexpected exception orphans the rest of a job.**
      `app/worker.py:255-259`, `app/downloader.py:58, 101`. `gather` without
      `return_exceptions` re-raises the first stray error (say, `mkdir` on a
      full disk) without cancelling the siblings, which keep downloading and
      holding slots after the job is marked failed and dropped from `RUNNING`
      — uncancellable, undeletable, items stuck animating, Retry says
      "nothing to retry".
      *Fix:* catch-all in `_process` that fails the item.
      **Fixed** as suggested; cancellation still propagates. Reproduced
      first with a download raising `OSError`.

- [x] **M17. Non-audio files are never removed from the inbox.**
      `app/inbox.py:213-279`. Covers, `.cue`, `.nfo` stay for ever and their
      folders are never pruned — contradicting "empty at rest", and feeding
      H7. *Fix:* once a settled folder has no audio left, clear it.
      **Fixed** by moving, not deleting, since an existing test (and
      principle 3) said the inbox must not delete somebody's files: known
      residue (images, cue, logs, playlists, checksums) in a settled folder
      with no audio, or loose at the inbox root, is moved to the
      workspace's `leftovers/`, then the folder is pruned. Unknown files
      stay. Runs on every drain, not only after something was filed.

- [x] **M18. Files the poller cannot file are invisible.**
      `app/main.py:253`, `app/inbox.py:294-296`. The poller discards
      `drain_all`'s failures; a failed file is thereafter counted as
      "waiting". An upload the poller reached first shows "1 still settling"
      for ever, with the reason only in the log.
      *Fix:* report remembered failures as failures, with their message.
      **Fixed** as suggested: `_unfilable` keeps the message beside the
      size, and every drain reports it as a failure until the file changes.
      The existing test had asserted the second drain reported nothing.

- [x] **M19. Upload-finish and the poller can file the same file at once.**
      `app/main.py:861`, `app/inbox.py:282-316`. Uploads are backdated, so
      both see them as settled; nothing serialises the two drains. Two track
      UUIDs are minted (the file keeps one), the loser reports a spurious
      failure, and a file with no album tag leaves an orphan registry row.
      *Fix:* a lock around `drain()`.
      **Fixed** as suggested (one lock for all workspaces; drains are
      quick). Reproduced first with two threads draining at once.

- [x] **M20. Uploads are read whole into memory before the size check.**
      `app/main.py:821-827`. Up to 200 MB per file — more, since the check
      comes after — in RAM on a Pi; a dragged folder sends several.
      *Fix:* check `file.size`, stream to disk with a running cap.
      **Fixed** as suggested: refused up front on the declared size, then
      copied a megabyte at a time to a hidden `.part` file with a running
      cap and renamed into place. Tests fail if the route reads the upload
      whole.

- [x] **M21. An album upload can be filed before its cover arrives.**
      `app/inbox.py:178-188`, `app/static/js/drop.js:164-185`. Each file is
      backdated as it lands, so the 15 s poller files tracks mid-upload;
      covers often upload last, and the album gets no folder art.
      *Fix:* upload covers first, or backdate only in `finish`.
      **Fixed** the second way: `finish` takes the batch and
      `inbox.release` backdates that drop's files, then drains; drop.js
      passes it. Reproduced first: the poller filed a track mid-drop.

- [x] **M22. Playlists from YouTube and other direct links are tagged from
      thin metadata.** `app/generic.py:114, 135-138`. Flat playlist entries
      carry no album or artist, and the comment's "full metadata is fetched
      at download time" is not true. A YouTube Music album link becomes N
      one-track albums, often under "Unknown Artist".
      *Fix:* extract each entry's full info before tagging, or use the
      playlist title as the album for album-type playlists.
      **Fixed** the first way: `generic._in_full` reads each playlist entry
      in full (three at a time) before tagging, keeping an entry's flat
      details if that fails. Costs one request per track while the job shows
      "resolving". Tested against a fake yt-dlp only; not tried on a real
      YouTube Music album, since nothing here reaches YouTube.

- [x] **M23. Version markers match inside words.** *Verified.*
      `app/matcher.py:95-97`. "Olive" contains *live*, "Demons" *demo*,
      "Obsession" *session*, "Discover" *cover*: a correct result loses 0.25,
      or a target containing one stops penalising real live/demo versions.
      *Fix:* match on word boundaries.
      **Fixed** as suggested. Reproduced first; whole words only, so a
      plural such as "Remixes" no longer counts either.

- [x] **M24. Session expiry is never detected over the WebSocket.**
      `app/main.py:2198-2201`, `app/static/js/ws.js:87`. `close(4401)` before
      `accept()` becomes an HTTP 403, and the browser sees 1006. After a
      restart the page shows "offline" and reconnects every 15 s instead of
      asking you to sign in. *Fix:* accept, then close with 4401.
      **Fixed** as suggested. Checked in headless Chromium against a
      fixture server restarted under a signed-in page: the old code stayed
      "offline", the fix shows sign-in.

### Platform

- [x] **M25. Saving Settings copies environment secrets into
      `config.toml`.** `app/config.py:166-168`. `save()` writes every
      editable key's live value, so a password or secret supplied by `DC_*`
      lands in plain text in the config volume on any save. An admin's edit
      to an environment-set key is silently reverted on restart. A test
      (`tests/test_config_and_auth.py:44`) asserts the current behaviour.
      *Fix:* remember which keys came from the environment; never persist
      them; show them as locked in the panel.
      **Fixed** as suggested: `config.FROM_ENV` records them at load; `save`
      refuses them and never writes their live values (a value already in
      the file is kept); `/api/settings` returns `locked` with each
      variable's name and the panel disables those fields. The test at
      `:44` now runs with nothing from the environment. A `config.toml`
      saved before this may already hold copied secrets.

- [x] **M26. A bad config write stops the container starting.**
      `app/config.py:138, 177-196`. `_toml_value` escapes only `\` and `"`; a
      newline makes invalid TOML. The write is not atomic. `load()` runs at
      import, so either failure is a crash loop.
      *Fix:* write a temp file and `os.replace`; serialise with a real TOML
      writer.
      **Fixed:** strings are escaped as TOML basic strings require (every
      control character), and the text is parsed before a temp file is
      swapped in with `os.replace`. No new dependency. `load()` still fails
      loudly on a file broken by hand. Reproduced first.

- [x] **M27. Smart playlists stop working when Navidrome's token expires;
      the session lives on.** *Expiry interval unverified on the Pi.*
      `app/navidrome.py:74-75, 320-352`. The bearer token is captured at
      sign-in and never refreshed (the refreshed one in
      `X-ND-Authorization` is ignored). With Navidrome's default 24 h
      timeout, Playlists shows "Navidrome refused that" from day two while
      everything else works.
      *Fix:* take the refreshed token from each response; on a 401, end the
      session.
      **Fixed** as suggested: `navidrome._native` keeps the token from each
      response's `x-nd-authorization`, and a 401 raises `SessionExpired`;
      the playlist routes then sign this session out and answer 401. A
      session that has not touched Playlists for longer than Navidrome's
      timeout still has to sign in again, which it is now told. Faked
      `requests`; the timeout on the Pi is still unverified.

- [x] **M28. The disk-audit loop dies on its first unexpected error.**
      `app/main.py:314-336`. `_library_roots` catches only
      `navidrome.Unavailable`; a `sqlite3.Error` ends the task for the life
      of the process, and Health's audit ages for ever.
      *Fix:* wrap the loop body as the other two loops do.
      **Fixed** as suggested. Reproduced first with `_library_roots`
      raising `sqlite3.OperationalError` once.

- [x] **M29. `state.db`'s shared connection is read without the lock.**
      *Effect unverified.* `app/store.py:177`; unlocked reads across
      `playcounts.py`, `overview.py`, `lastfm.py`. Readers in other threads
      can see `take()`'s uncommitted writes; a manual snapshot and the timer
      can run `take()` together and double-count the run log.
      *Fix:* per-thread connections in WAL mode, or a read context manager
      that takes the lock; a mutex for `take()`.
      **Partly fixed, left open:** `take()` now holds a mutex, so a forced
      reading and the timer run one after the other (reproduced first: two
      overlapping readings). Readers still share the connection unlocked;
      moving to per-thread WAL connections is a large change for an effect
      nobody has seen, so it waits for evidence. Round 2 found the
      evidence: see **2M20**.

- [x] **M30. Resolving a duplicate silently loses other users' ratings and
      play counts, and your own play count.** `app/duplicates.py:144-151, 207-210`.
      Only other users' *stars* are checked; their ratings and plays go into
      quarantine with the removed copy, and the caller's play count is never
      migrated. *Fix:* treat any other user's rating or plays as
      unmovable; show your own play loss in the confirm.
      **Fixed** as suggested: `held_by_others` (renamed from
      `starred_by_others`) now names anyone with a star, rating or plays,
      and protects the copy; each copy carries your own `plays`, and the
      confirm states the count lost from Navidrome. Reproduced first.

- [x] **M31. Auto-resolve applies a different set than it previewed.**
      `app/main.py:1200-1219`, `app/duplicates.py:669-685`. `?apply=true`
      recomputes, so groups that appeared since the preview — for instance
      during an import — are resolved unseen. Synchronous in the request,
      with up to two Navidrome calls per group.
      *Fix:* resolve only the previewed keys; refuse during an active job or
      drain; run it as an operation.
      **Fixed** as suggested: the preview returns every group's key and
      keeper; `POST /api/duplicates/auto/apply` takes them as a JSON body,
      resolves only those (skipping any whose keeper changed), runs as the
      `dupes-auto` operation, and answers 409 while a job runs or the inbox
      is filing into the person's libraries. Reproduced first (one group
      previewed, two resolved). Not clicked through in a browser.

- [x] **M32. "Keep both" is not scoped to the caller and its input is
      unbounded.** `app/main.py:1177-1183`, `app/store.py:224-233`. Any user
      can dismiss any key with any size of note; in a shared library one
      person's decision hides the group from the other.
      *Fix:* validate the key against the caller's groups; cap the note;
      record who decided.
      **Fixed** as suggested: the key must be one of the caller's groups
      (404), key and note are capped (64 and 500), and `duplicate_dismissed`
      gains `decided_by` in its key - rebuilt by a migration, with older
      rows kept for everyone. Each person sees only their own decisions.

- [x] **M33. Sessions never pick up privilege changes.** `app/auth.py:69-78, 130-137`.
      Admin status and libraries are fixed at sign-in (libraries re-read only
      when empty), and the sliding lifetime never ends while used. A demoted
      admin stays admin; a revoked library stays editable.
      *Fix:* re-read both every few minutes; cap absolute lifetime.
      **Fixed** as suggested: `navidrome.account` reads `is_admin` and the
      libraries (raising, not answering "none", when the database is
      unreadable); `auth.get` re-reads every 5 minutes (every minute with no
      library), signs out an account that no longer exists, and keeps what
      it had on a read failure. Sessions end 30 days after sign-in.

- [x] **M34. The set-aside list mislabels older entries.**
      `app/duplicates.py:596-658`. Only the newest 2,000 ledger rows are
      joined, and the walk is cut at 500 in alphabetical order before sorting
      by date — older files read "no record", and the list is an alphabetical
      slice, not the latest. *Fix:* look up the files found; sort before
      cutting.
      **Fixed** a little differently: every ledger row is joined (not a
      lookup per file - the ledger is small), and the full list is built,
      sorted newest first, then cut; totals describe everything.
      Reproduced first.

- [x] **M35. `unfuse --plan FILE` keeps only the last library's plan.**
      `app/unfuse.py:501-502`. The "reversible" plan is overwritten per
      workspace. *Fix:* one file per library.
      **Fixed** as suggested: `plan.json` becomes `plan-<library id>.json`,
      and each path is printed.

### Performance

- [x] **M36. Library pages walk the whole library on every request.**
      `app/library.py:636-651` and the listing. Opening one album reads every
      `media_file` row; *Combine* does that once per selected album in
      parallel; every inline save triggers 2–5 full walks (refresh plus the
      attention tab); `album_ids` walks per edit and per bulk *Mark
      reviewed*. *Fix:* filter by folder in SQL; memoise the listing on
      Navidrome's database stamp (`memo.py` exists).
      **Fixed** as suggested: `library._cached_load` memoises the album list
      on `library_stamp` plus the person's annotation totals and hands out
      copies; `tracks()` narrows by folder with an escaped `LIKE`. Combine
      still reads each album's tracks, now one folder each. Reproduced
      first (two listings, two walks).

- [x] **M37. The track index is rebuilt on almost every request while music
      plays.** *Cost on the Pi unmeasured.* `app/playcounts.py:470-494`,
      `app/overview.py:386`. The change check is the database and WAL files'
      mtime and size, and Navidrome writes on every play, so a full
      `media_file` scan with three JSON lookups per row runs on the request
      path. *Fix:* detect changes with `max(updated_at), count(*)` on
      `media_file`.
      **Fixed** as suggested: `navidrome.library_stamp` is `count(*)`,
      `max(updated_at)` and `sum(missing)` on media_file plus missing
      folders; the track index and Home's collection count are keyed on it.
      Without an `updated_at` column it falls back to the file stamp.
      Reproduced first (a play rebuilt the index). Cost on the Pi still
      unmeasured.

- [x] **M38. The cover survey costs ~70k filesystem calls, and can run twice
      at once.** `app/covers.py:153-158, 263-268`, `app/static/js/library.js:263-276, 811-826`.
      *Fix:* one `scandir` per folder; share one in-flight request in the
      browser.
      **Fixed** as suggested: `covers._look` does one `scandir` per album
      and `current()` takes the covers it found; measured locally at 28
      calls per known album before, one listing after. The page shares one
      in-flight request.

- [x] **M39. Health does ~8 full scans and the duplicate finder per
      request, polled every five minutes per open tab.** `app/health.py`,
      `app/static/js/main.js:152`. *Fix:* cache per user for a minute on the
      database stamp.
      **Fixed** as suggested: `health._from_navidrome_cached`, keyed on
      `library_stamp`, the duplicate decisions, the audit's time and a
      one-minute clock; each report deep-copies it. Reproduced first.

---

## Low

### Getting music in

- [x] **L1.** Spotify links without `https://` and `spotify:` URIs are
      searched instead of queued (`browse.js:34-36`), though the server
      accepts them; `spotify.link` short links go to yt-dlp and fail;
      `/embed/` links are not recognised (`spotify.py:22-25`).
      **Fixed:** the page queues bare `open.spotify.com/`, `spotify.link/`
      and `spotify:` forms; the server routes by `spotify.is_spotify`,
      accepts `/embed/`, follows short links when resolving
      (`spotify.expand_short`), and stores the canonical https link.
      Reproduced first. Not tried against a real short link: the redirect
      is assumed, with the page body as a fallback.
- [x] **L2.** Resolving a direct link ignores `cookies.txt` (`generic.py:131-139`),
      so an age-gated video fails at resolve though it would download.
      **Fixed:** `generic._options` adds `cookiefile` for the playlist
      read and each entry's full read, as the download does. Reproduced
      first (no cookie option on either read).
- [x] **L3.** `.incomplete/<job>` folders left by a crash are never removed
      (`inbox.py:110-117`). *Fix:* clear `.incomplete/` at start-up.
      **Fixed** as suggested: `inbox.clear_scratch`, run once from the
      lifespan, empties every workspace's `.incomplete/`. Delivered files
      are in the inbox proper and untouched.
- [x] **L4.** If filing fails after the move into the inbox, the item is
      marked failed but the poller files it two minutes later; Retry then
      makes a second copy (`worker.py:181-186`).
      **Fixed:** `inbox.deliver` moves the file back to scratch space when
      filing raises, so the job's discard clears it and Retry fetches it
      once. If even that move fails it stays for the poller, logged.
      Reproduced first.
- [x] **L5.** The worker ignores `Filed.identified` (`worker.py:188`): a track
      filed without UUIDs shows Done.
      **Fixed:** the item keeps `complete` (it is filed and playable) but
      carries a `warning`; its row reads "Done, no identity" and the
      finished card counts them, in the warning colour. Reproduced first.
- [x] **L6.** A tag write failure leaves the file tagless (tags were deleted
      first) and filed as `Unknown Artist/Unknown Album/<hex id>.mp3`, shown
      as Done (`worker.py:167-176`, `tagger.py:31`).
      **Fixed:** the tagger clears the old frames in memory, so a failure
      before the save leaves the file as it was; a tagging failure now
      fails the item ("Could not tag the download") and the file is
      cleared with the job's scratch space. Reproduced first.
- [x] **L7.** Drops and uploads never ask Navidrome to scan (`main.py:253, 861`);
      only downloads do.
      **Fixed:** the inbox loop and `finish_upload` call `navidrome.notify`
      when they filed something. Reproduced first.
- [x] **L8.** Retry ignores the five-active-jobs limit (`main.py:744-768`); the
      limit check itself races across concurrent requests (`main.py:604-619`);
      retry resets items before it can fail (`main.py:757-763`).
      **Fixed:** `_check_room` is shared by create and retry; create runs it
      after its one await, so the count and the new job have no await
      between them; retry resolves the workspace and checks room before
      resetting anything. Reproduced first (two concurrent creates both
      admitted).
- [x] **L9.** Changing `concurrency` mid-job builds a second semaphore, so
      old and new jobs together exceed it (`worker.py:51-56`).
      **Fixed:** `worker.Gate` is one condition-based gate whose size is
      read from the setting each time a download asks. Lowering it holds
      new downloads until enough finish; raising it lets more in as each
      finishes. Reproduced first (four at once after lowering 3 → 1).
- [x] **L10.** An item's error survives a successful in-run retry and shows
      as a tooltip on a Done row (`worker.py:88, 115, 188`).
      **Fixed:** completing an item clears `error`. Reproduced first.
- [x] **L11.** Browse and Drop never send `library_id`: a multi-library
      account always uses its first library, with no way to choose.
      **Fixed:** an *Into* picker on Browse and Drop, shown only for more
      than one library, shared between them and remembered per browser
      (`core.libraryPicker`); jobs, uploads and the upload finish send its
      `library_id`. Checked in headless Chromium against stubbed APIs (two
      libraries: sends 3 and remembers it; one: hidden, sends null). The
      "in library" markers still read the first library.
- [x] **L12.** The Drop file picker's `accept="audio/*"` hides covers and,
      on some systems, `.ape`/`.wv` (`index.html:220`).
      **Fixed:** `drop.js` sets `accept` to `audio/*` plus every audio and
      cover extension it already filters by; a test keeps those lists equal
      to the server's. Checked in headless Chromium.
- [x] **L13.** The ✕ on an active job deletes it (history and Retry gone);
      `POST /api/jobs/{id}/cancel` is never called.
      **Fixed:** the ✕ on an active job calls cancel; the job lands in
      *needs a look* with Retry, and its ✕ then clears it. A retried job
      is put in `RUNNING` when its task is created, so it can be cancelled
      at once. Checked in headless Chromium with a stubbed socket.
- [x] **L14.** Two jobs filing the same track can both choose the same free
      name; the second move overwrites the first (`filer.py:527-535`).
      *Race window unverified.*
      **Fixed:** `filer.claim` reserves the name with an `O_EXCL` create
      and the move replaces that placeholder (removed again if the move
      fails). Reproduced first with two threads and a slowed move: one
      file lost before, both kept after. The inbox hop keeps
      `unused_name`, since its names are unique item ids and a placeholder
      there could be picked up by the poller.
- [x] **L15.** An oversized playlist is fully resolved (≈200 requests for
      10,000 tracks) before the 500-track limit refuses it
      (`main.py:537`, `spotify.py:185-193`).
      **Fixed:** `spotify.resolve` refuses from the first page's `total`
      and `generic.resolve` from the flat list's length, before any further
      request; the job resolver passes `MAX_TRACKS_PER_JOB`, and its own
      check stays as a backstop. Reproduced first (paging and hydrating
      ran before the refusal).
- [x] **L16.** The same cover is fetched and squared once per track
      (`tagger.py:65-72`).
      **Fixed:** `covers.squared(url)` keeps the last eight squared covers
      and lets one track fetch while the others wait; failures are not
      kept. Reproduced first (six tracks, six fetches; now one).
- [x] **L17.** An upload path segment starting with `.` is never filed —
      hidden folders are skipped (`main.py:793-794`).
      **Fixed:** folder segments lose their leading dots (a `..` segment
      is now dropped rather than becoming `unknown`); a file whose own name
      starts with a dot, macOS's `._` companions mostly, is refused with a
      400 and skipped by the Drop page. Reproduced first.
- [x] **L18.** `_delivering.add` happens after the move, leaving a window
      at `inbox_quiet_seconds = 0` (`inbox.py:134-136`).
      **Fixed:** `deliver` chooses the inbox name and marks it as being
      delivered before moving onto it. A move that fails partway now
      removes its half-written copy rather than moving it back over the
      intact original. Reproduced first.
- [x] **L19.** `audio_bitrate = 320` re-encodes YouTube's ~130–160 kbps Opus
      at twice the size for no gain. Consider 192, or keeping Opus/M4A.
      **Fixed** by choice (192 when the source is no better): the MP3
      step reads the downloaded format's bitrate and encodes at 192 when
      it is at most 192; a better or unreported source keeps the setting,
      and a VBR level is left alone. Checked with a real YouTube download:
      Opus at 106 kbps became a 192 kbps MP3, where it was 320.

### Library and listening

- [x] **L20.** Quarantine has no "still arriving" check and leaves the
      emptied folder (`main.py:1439-1509`).
      **Fixed:** both quarantine routes answer 409 while
      `inbox.receiving` the folder. `duplicates._leave` runs after each
      file is set aside: once a folder has no audio, its folder covers
      move to the matching place in the quarantine and the folder is
      removed, walking up through emptied parents; any other file keeps
      it. Reproduced first.
- [x] **L21.** The barred-cover memory is lost on restart; `barred_known`
      ignores the stamp, so a squared cover stays flagged until the next
      survey; and a card already flagged *Review* never gets the *Cover*
      flag (`library.js:273` tests any `.tone-warn`).
      **Fixed:** the survey's answers are saved to
      `config/.cover-survey.json` after each survey (pruned to the folders
      it visited) and read back on first use; `covers.apply` forgets the
      album it squared; the page looks for its own `.flag-cover`.
      Reproduced first.
- [x] **L22.** Cover fetching has no size cap and no image check; an HTML
      error page would be embedded as art (`covers.py:43-59, 105-110`).
      **Fixed:** `covers.fetch` reads at most 25 MB and returns None for
      anything larger or anything Pillow cannot verify as an image, so
      every caller (downloads, Fetch cover, combine) gets an image or
      nothing. Reproduced first.
- [x] **L23.** The album editor's merge search spans every library; picking
      another library's album makes a new album instead of merging
      (`library.js:1159`).
      **Fixed:** `GET /api/library` takes `library_id`, and the merge
      search passes the album's own. Reproduced first.
- [x] **L24.** An artist page stops silently at 200 records (`library.js:414`).
      **Fixed:** the artist page keeps paging until it has the total.
      Checked in headless Chromium with a stubbed 450-record artist: 200
      cards before, 450 after.
- [x] **L25.** `GET /api/playcounts` is not admin-only and shows imported
      totals summed across accounts — on a two-person install, one person's
      total (`main.py:2093-2103`). Nothing in the UI uses it.
      **Fixed:** admin-only. Kept rather than removed: it is the one check
      that the collector is running.
- [x] **L26.** "This month" and "this year" come from UTC while plays are
      local: from 8pm New York time on the last day of a month, Home shows
      next month's empty bucket (`overview.py:283, 341, 565`).
      **Fixed:** `overview._now()` is local (`playcounts.zone()`), used
      for the month list and the year; the cache key drops the UTC day.
      Reproduced first with the clock frozen at 20:30 on 31 October in
      New York.
- [x] **L27.** A play with no `play_date` falls back to the reading's UTC
      stamp, bucketed as if local (`overview.py:107`).
      **Fixed:** the stand-in reading stamp goes through `local_stamp`
      like a play date; a bare nightly-era date is left as it is.
      Reproduced first.
- [x] **L28.** The headline "You've played N tracks this month" counts plays
      (`overview.py:456-460`).
      **Fixed:** it reads "You've racked up N plays this month". The year
      line keeps counting different tracks.
- [x] **L29.** The *In year* tile is the calendar year; tapping it shows the
      last 365 days (`home.js:232-236`).
      **Fixed:** the tile opens the statistics on its calendar year through
      a new `listening.selectDates`. Checked in headless Chromium: it asked
      for `days=365` before and `start=2026-01-01&end=2026-12-31` after.
- [x] **L30.** A forced snapshot does not warm Home's cache (`main.py:2176-2182`).
      **Fixed:** `POST /api/playcounts/snapshot` warms Home for the people
      it found new plays for, as the timed reading does. Reproduced first.
- [x] **L31.** An empty monthly chart labels its peak "1" (`charts.js:32, 138`).
      **Fixed:** the label and the chart's accessible name use the real
      peak; with no plays there is no peak label and the name says "with
      no plays yet". Checked in headless Chromium (old: "1"; new: none,
      and "7" for a series peaking at 7).

### Platform

- [x] **L32.** `/docs`, `/redoc` and `/openapi.json` are open without
      sign-in (`main.py:354`).
      **Fixed:** the session middleware gates them with the API; a
      signed-in person can still read them. Reproduced first.
- [x] **L33.** The cookie's 14-day `max_age` is set once at sign-in while the
      server session slides; active users are signed out on day 14
      (`main.py:462-466`).
      **Fixed:** the session middleware re-sends the cookie once a day
      while a session is used, with a max-age that never passes the 30-day
      cap; sign-in shares the same `_send_cookie`. Reproduced first.
- [x] **L34.** `tools/` is not in the image, but `tools/fingerprint.py`'s
      usage says to run it there; it also writes the MusicBrainz recording
      id into the *AcoustID Id* frame (`fingerprint.py:193`), never
      checkpoints failures, and walks the quarantine.
      **Fixed:** the image copies `tools/`, and the usage adds `--user`;
      the *Acoustid Id* frame gets AcoustID's own track id, and an earlier
      run's misfiled one is removed when the file is next passed over; no
      match and unreadable are checkpointed (`--retry-failed` asks again),
      a lookup that never answered is not. The walk already skipped the
      quarantine; `--from-list` now does too. Reproduced first.
- [x] **L35.** Settings has no `beets_enabled` control, and a non-secret
      field (Navidrome URL, Spotify id) can never be cleared
      (`index.html:466-476`, `settings.js:42`).
      **Fixed:** a checkbox for `beets_enabled`, sent ticked or not; the
      Spotify client id and the Navidrome URL and username are sent empty
      when emptied, which clears them (the server already accepted that).
      A blank secret, number or bitrate is still left out. Checked in
      headless Chromium.
- [x] **L36.** CLI tools run as root under `docker exec`, leaving root-owned
      files in `/config`. *Fix:* warn or refuse when `geteuid() == 0`.
      **Fixed:** `app/cli.not_as_root` refuses, naming the `docker exec -u
      downloader` form; `DC_ALLOW_ROOT=1` overrides. Called first by
      backfill, lastfm, reindex, survey and unfuse.
- [x] **L37.** Updating a playlist when Navidrome is unreachable is a 500,
      not a 502 (`main.py:1270`).
      **Fixed** already, by M27 (`98dedc3`), which wrapped the ownership
      check; a test now holds it.
- [x] **L38.** CSRF rests on `SameSite=Lax` alone, which ignores ports — a
      page on another port of the same host (Navidrome, Calibre) could fire
      body-less POSTs: `/api/duplicates/auto?apply=true`,
      `/api/health/audit`, `/api/library/rescan`, `/api/playcounts/snapshot`.
      `/ws` checks no Origin. *Unverified in practice.* *Fix:* reject
      non-GET requests whose `Origin`/`Sec-Fetch-Site` is not same-origin.
      **Fixed** as suggested: `main.same_origin` reads `Sec-Fetch-Site`,
      falling back to `Origin` against `Host`/`X-Forwarded-Host`; the
      middleware answers 403 to a POST, PUT, PATCH or DELETE that fails it,
      and `/ws` closes with 4403. A request with neither header (curl, a
      script) passes, since it carries no cookie by accident.
- [x] **L39.** Sign-in returns raw exception text (internal hostnames) to
      unauthenticated callers and is not rate-limited (`main.py:454-457`).
      **Fixed:** an unreachable or unconfigured Navidrome gets a fixed
      message, the reason going to the log; ten failed sign-ins from one
      address in ten minutes get a 429 until the window passes, and a
      success clears the count. Reproduced first.
- [x] **L40.** Cover art is served `Cache-Control: public` though it is
      owner-checked (`main.py:1692`); use `private`.
      **Fixed** as suggested.
- [x] **L41.** Blocking filesystem and database work runs on the event loop
      inside `async` routes and the auth middleware (`_library_roots`,
      `album_dir`, `audio_in`, `settled`, `auth.py:130-137`).
      **Fixed:** the session middleware and the socket look the session up
      in a thread; the audit loop reads the library list and each
      staleness check in one; every library route runs its checks
      (`for_session`, `album_dir`, `track_path`, `audio_in`, the one-album
      tag read, `receiving`) in one before its work. `_browsing_library`
      no longer builds a workspace to learn a library id. `settled` is no
      longer called from a route. A test watches each helper for a call on
      the loop's thread; all nine cases failed before.
- [x] **L42.** Arbitrary server-side fetches: direct links and thumbnails
      reach any URL a signed-in user supplies, including LAN addresses, with
      the first 200 characters of errors returned; `choosable()` is checked
      before redirects only.
      **Fixed:** `app/netguard.check` refuses anything but http(s) to a
      host whose every resolved address is public (`is_global`). Direct
      links are checked when queued, covers before fetching and at every
      redirect, and offered covers stay on their hosts at every hop too.
      Not closed: yt-dlp follows a site's own redirects unchecked, and a
      name that changes its answer between the check and the fetch (DNS
      rebinding) can still get past.
- [x] **L43.** `with sqlite3.connect()` does not close connections (it only
      commits); use `contextlib.closing`.
      **Fixed** in one place rather than twenty: `navidrome.open_db`
      returns a connection whose `with` also closes it, and a connection
      that fails its first query is closed before raising. state.db's one
      long-lived connection is meant to stay open. Reproduced first.
- [x] **L44.** `audio_bitrate` is unvalidated text and `rate_limit_sleep`
      unbounded (`config.py:48-51`).
      **Fixed:** `audio_bitrate` must be 32-320 kbps or a VBR level 0-9
      (a trailing `k` is accepted and dropped); `rate_limit_sleep` is
      capped at 300 seconds. Settings refuses anything else with the
      reason. Reproduced first.
- [x] **L45.** Focus rings are `box-shadow`, which vanish in forced-colours
      mode; there is no `forced-colors` rule (`style.css`).
      **Fixed:** under `forced-colors: active`, `:focus-visible` gets a
      2px `CanvasText` outline. Checked in headless Chromium with forced
      colours emulated: the focused button's outline was `none` before and
      `solid 2px` after.

---

## Readability

- [ ] **R1. Split `app/main.py` (2,287 lines) into routers.** Proposed:
      `main.py` (~150 lines: app, middleware, lifespan, `/healthz`, `/`,
      static); `api/deps.py` (`current_session`, `admin_session`, a
      `space_for` dependency replacing ~12 copies of one try/except, an
      `album_target` helper); `jobs.py` (job state, broker, run/stop logic,
      no routes); `background.py` (the three loops); and routers
      `api/auth`, `api/jobs` (+ `/ws`), `api/inbox`, `api/settings`,
      `api/browse`, `api/health`, `api/duplicates`, `api/playlists`,
      `api/library_read`, `api/library_edit`, `api/listening`. Give each
      router `dependencies=[Depends(current_session)]`, so auth stops
      depending only on a path prefix.
- [x] **R2. Split `app/static/js/library.js` (~2,300 lines)** along its own
      sections: list, attention, drawer and editing, combine. Pass options to
      `showProgress` instead of special-casing the string "ReplayGain"; share
      the duplicated cover-survey fetch; use the existing `button()` helper in
      `renderBar`.
      **Fixed:** library.js is now five modules: `library-shared.js`
      (elements, the view state, helpers, cover art, the selection, and the
      state more than one module replaces, on one `libraryState` object),
      `library.js` (the list, artists, select mode, controls),
      `library-attention.js` (Needs attention, bulk actions, ReplayGain),
      `library-drawer.js` (the album panel, editing, covers, matching,
      quarantine) and `library-combine.js`. The shared module imports none of
      the others, so the rest can import each other's functions safely.
      `showProgress` takes `{label, stopUrl, ordinal}`; the list and Needs
      attention share one `loadCoverSurvey`; `renderBar` uses the button
      helper. To avoid clashing with other modules' locals, the shared helpers
      are `actionButton`, `editField` and `viewing`. Checked in headless
      Chromium with stubbed APIs: list, progress with Stop, cover flags, the
      panel and editor, select mode and Combine, Needs attention and Artists
      all behave the same as the single file did, with no page errors.
- [x] **R3. Stale text describing the removed staging, ledger, nightly and
      beets-files-everything design.**
      - User-facing: "nightly" at `index.html:130, 148`; the Settings note
        "New downloads are fingerprinted without it" (`index.html:477-478`);
        Health's hint "Run the stamper" (`health.py:140-141`).
      - Docstrings and comments: `workspace.py` (module, class,
        `existing`, `require_mounted`), `tagger.py:1-7`, `matcher.py:35-38`,
        `downloader.py:68-69`, `generic.py:115-116`, `config.py:67-69`,
        `operations.py:3-19`, `operations.js:3-5`, `beets_match.py:16-18`,
        `beets_runner.py:162-164`, `reindex.py:38-40`, `playcounts.py:1-23`,
        `overview.py:9-23, 53-54`, `lastfm.py:228-230`, `main.py:259-263`,
        `home.js:132, 263`, `listening.js:3-8`, `index.html:93-98, 123`,
        `uuidtags.py:11` and `survey.py:23` / `registry.py:5`
        (`tools/ensure_uuid.py`), `diskaudit.py:10`, `inbox.deliver`'s
        docstring contradicting the `_delivering` comment.
      - `library.js:2233-2246` handles a beets lock and `busy` result that no
        longer exist; `importSummary` says "keeps the album identity it had"
        even after a merge.
      **Fixed:** every cited spot reworded to what happens now. Home says
      "play-count readings", not nightly snapshots; the AcoustID note says
      Find matches works without a key; Health's unstamped hint says to save
      the album in Library (filing stamps it) since the stamper script is
      gone, and the stale-index hint no longer claims stamping keeps mtime
      (only tools/fingerprint.py does now). The workspace, operations,
      playcounts and overview docstrings describe the inbox poller and
      five-minute readings; inbox.deliver now credits the `_delivering` claim,
      not the quiet period, for keeping the poller off a delivered file. In
      library.js the dead `busy` branch is gone and a retag's notice no longer
      promises the album kept its identity, since after_retag can join it to
      an existing one. Historical notes that say "used to" were left.
- [x] **R4. Dead code:** `playcounts.last_complete_day` and `taken_on()`,
      `registry.album_uuid_for` (tests only), the `filing` item status (only
      the front end knows it — set it before `deliver`, or drop it).
      **Fixed:** `last_complete_day`, `taken_on` and `album_uuid_for` are
      deleted; their tests now ask the run log or `registry.known` /
      `uuid_for_key` directly, and the two tests that only pinned
      `last_complete_day`'s date arithmetic went with it. The worker now sets
      `filing` before handing a download to the inbox, so the Filing state
      Browse and Downloads already draw is reachable; a new test fails on the
      old worker, which went straight from tagging to complete.
- [x] **R5. Duplication:** `_download_with_retries` and
      `_match_with_retries` share one backoff loop (`worker.py:68-117`);
      `create_job` still writes a beets config even when beets is disabled
      (`main.py:614-617`); `Workspace.prepare` makes `beets_dir` on every
      15-second poll; `health.py:536-537` attaches a row to `sections[1]` by
      position; the stamped count is computed twice (`health.py:114, 518`);
      `main.py:1967-1968` double-counts tracks inside selected folders.
      **Fixed:** the two retry loops are one `_retrying` helper taking the
      exception worth retrying. Queueing a download prepares the workspace and
      no longer writes a beets config; `prepare` stops making `beets_dir`,
      which `ensure_config` now makes when Find matches first needs it. Health
      counts stamped tracks once in `_from_navidrome` and hands the count to
      the Identity section, and attaches the stale-index and duplicates rows
      by section title: with a section failed, they used to land in "Checks
      unavailable". The combine route counts distinct files, so a single
      chosen both as its album and as a track is one track and is refused. New
      tests for each fail on the old code.

---

## Test gaps

- **No HTTP-level tests at all** — no `TestClient` anywhere. The session
  gate, `admin_session`, secret masking, ownership 404s for other users'
  jobs and playlists, upload sanitising and size limits, the `/ws` close,
  cancel and retry are all untested end to end.
- **Tests that check constants, not behaviour:**
  `tests/test_config_and_auth.py:67-93` checks membership in `EDITABLE` and
  `SECRETS`; `:44` asserts the secret leak in M25.
- **Missing cases** that would have caught findings above: the matcher's
  normaliser on real titles (C2); quarantine exclusion in diskaudit, survey
  and unfuse (C4, H6); `import_chosen` with `applied: false` (H1);
  operation visibility per user (H3); Listening ranges starting before the
  first snapshot and Home's first plays (H4, H5); JS operator names against
  `OPERATORS` (H8); config escaping and atomic writes (M26); the audit loop
  surviving an exception (M28); token and cookie refresh (M27, L33).

---

# Round 2 — 2026-10-04

A second read-only review, at commit `f900521`, after every round-1 item
except R1 and M29 had been fixed. Five reviewers worked in parallel on
ingest, the Library, listening and integrations, the platform, and the
frontend. They were told not to repeat round 1, and to look for what it
missed, for gaps or regressions in its fixes, and for structural problems.
Most findings were reproduced against the real functions with temp
directories and in-memory databases, or in headless Chromium against stubbed
APIs. Nothing was run against the Pi or the network.

Items are numbered `2H`, `2M`, `2L` and `2D` (design) so they cannot be
confused with round 1's. Severity and tick-off rules are the same as above.

Nothing was rated Critical. All 57 routes and the websocket are guarded, no
cross-account leak was found, and the frontend has no HTML sinks at all. The
R2 split of `library.js` lost nothing.

## High

- [x] **2H1. An incoming file's album UUID is adopted under a new name even
      when another album already owns it.** *Verified.*
      `app/registry.py` (`uuid_for_key`), `app/filer.py` (`file_track`,
      `after_retag`). A track retagged to another album in Mp3tag or Picard
      keeps its `NAVIDROME_ALBUM_UUID`. Dropped back in, the new name is
      unregistered, so the UUID on the file was recorded for it: two keys,
      one UUID, two folders, one fused album in Navidrome.
      **Fixed** (`baff4c2`): `on_miss` is honoured only when no other key in
      that library already holds the UUID; otherwise a fresh one is minted.
      The check is inside the same lock. New test fails on the old code.

- [x] **2H2. Every request competes with long work for one 8-thread pool.**
      *Verified locally, not on the Pi.* `app/main.py:484` (middleware),
      `app/worker.py:126,142,216,234`, `app/operations.py:142`,
      `app/spotify.py:53-65`. Every blocking call uses `asyncio.to_thread`,
      i.e. the default executor: `min(32, cpus + 4)` = 8 threads on the Pi.
      Since L41 the session lookup for every request, `/healthz` included,
      needs one. Downloads (3 by default, Settings allows 10), resolving
      jobs, a ReplayGain run (hours), the disk audit, Find matches and apply
      each hold one for their whole length. With eight busy, the UI,
      sign-in, the websocket and the Docker healthcheck hang with nothing
      logged. The Spotify client's `retries=3` honours `Retry-After`, so one
      long 429 pins a thread for the whole wait, per search and per resolve.
      `operations.py`'s docstring assumes forty threads (anyio's pool, which
      this code never uses). A regression in effect from L41.
      *Fix:* give downloads and operations their own executors and keep the
      default pool for the request path; skip the thread hop when there is
      no cookie; build the Spotify client so it does not sleep on
      `Retry-After`.
      **Fixed:** new `app/threads.py` runs long work on a pool of its own
      (32 threads; the download gate and per-person operations still bound
      it). Moved onto it: everything in `worker.py`, every operation, the
      three background loops, startup's scratch clear, resolving a job, an
      upload's drain, cover candidates and the forced snapshot. The
      middleware skips the thread hop when there is no cookie. The Spotify
      client gets its own session: three retries with a short backoff,
      `Retry-After` ignored, and then the existing 502 "Spotify error".
      Reproduced first with the default pool shrunk to one thread; new
      tests fail on the old code. Cancelling still does not stop the
      thread: that is 2M2.

- [x] **2H3. An operation that finishes while the socket is down is never
      reported.** *Verified in Chromium.* `app/static/js/ws.js:39-58,71-75`,
      `app/static/js/main.js:147,158-168`, `app/main.py:2577`. Operations
      are announced once over the socket. On reconnect the server sends a
      jobs snapshot only, and `resumeOperations()` runs only in `start()`.
      After a phone sleep or a Wi-Fi blip, the page says "live" but keeps
      "ReplayGain: 1 of 3" with a Stop button, "Re-read files" disabled,
      and "Asking MusicBrainz…" unresolved, until a reload.
      *Fix:* on every reconnect after the first, re-fetch `/api/operations`
      and pass each through `showOperation`; or put operations in the
      snapshot.
      **Fixed:** `operations.js` remembers which operations the page has
      shown as running, and `catchUpOperations` runs on every socket open
      (ws.js's new `onOpen`), not only at start-up. It shows anything
      running, plus the outcome of anything the page was watching, so a
      missed finish is delivered once. Reproduced first in Chromium with a
      stubbed socket (the progress line and the disabled audit button
      stayed); the same scenario is clean after. A static test pins the
      wiring.

- [x] **2H4. A name starting with a dot may be filed where Navidrome never
      scans.** *Our side verified; Navidrome's side unverified.*
      `app/filer.py:65-73` (`sanitize`), `app/inbox.py:326`,
      `app/walk.py:39-43`. `.38 Special` and `.5: The Gray Chapter` are
      filed as `.38 Special/…` and `Slipknot/.5_ The Gray Chapter/`. Two
      reviewers recall that Navidrome's scanner skips directories starting
      with a single dot; if so, the album is reported Done and never
      appears, and Health counts it as stamped-but-unscanned. Separately
      (verified), a dot-folder dropped into the inbox over SMB is skipped by
      `waiting()` and never reported (L17 fixed browser uploads only).
      *Check first:* make a dot-folder in a test library on the Pi and scan.
      *Fix:* replace a leading single dot in `sanitize`; report hidden
      folders in the inbox rather than pass over them.
      **Fixed:** confirmed from Navidrome's source
      (`scanner/walk_dir_tree.go`, `isDotEntry`): with `IgnoreDotFolders`
      on, the default, a folder or a file whose name starts with exactly one
      dot is skipped; two or more dots are scanned. `sanitize` now turns a
      single leading dot into `_` (`.38 Special` → `_38 Special`) and keeps
      `...And Justice for All`. The upload route keeps the dot on the file's
      own name so it can still refuse macOS's `._` companions, and now
      accepts a `...` title it used to refuse. New tests fail on the old
      code. Not done here: the inbox still skips hidden folders, on purpose,
      since `.incomplete` and a `.Trash` folder live there. Reporting what it
      skipped is part of 2M6. Albums already filed under a dot-folder on the
      Pi stay where they are until edited.

## Medium

### Jobs, shutdown and the platform

- [x] **2M1. A setting that validates is stored un-normalised: saving
      bitrate `320k` fails every download until a restart.** *Verified.*
      Regression from L44 + L19. `put_settings` validated a throwaway copy
      and `config.save` stored the raw text; `bitrate_for` passed `"320k"`
      through and `float()` raised in the post-processor.
      **Fixed** (`26d24da`): the values stored are the validated model's.
      New test fails on the old code.

- [x] **2M2. Cancelling a job does not stop its threads.** *Verified with
      the real `_run`/`_stop_job` and a threaded fake downloader.*
      `app/worker.py:126,234`, `app/main.py:664-684,812-839`, comment at
      `app/main.py:71-74`. `task.cancel()` ends only the asyncio task.
      Cancelled while filing, the item reads `cancelled` with no path, the
      thread files it anyway, and Retry files it again as `(2)` with a second
      track UUID. Cancelled mid-download, the gate slot is freed and the
      scratch folder removed while yt-dlp keeps writing, so more than
      `concurrency` downloads run and `.incomplete/` is recreated. The
      cancellation tests use `asyncio.sleep` for the download, so they
      cannot see this.
      *Fix:* a per-job `threading.Event` the yt-dlp progress hook raises on;
      release the gate when the thread returns; never mark an item cancelled
      once delivery has started.
      **Fixed:** `downloader.download` takes a `stop` event, checked by a
      progress hook and a post-processing hook that raise yt-dlp's own
      `DownloadCancelled` (it re-raises that out of `download()`); it
      surfaces as `downloader.Cancelled`, never retried. The worker runs
      each thread step through `_until_done`, which on cancellation sets the
      event and waits for the thread to return before the cancellation goes
      on, so the gate slot and the scratch folder outlive the thread. Filing
      is waited for, not stopped, and the item is recorded as complete with
      its path. New tests fail on the old code, including one against the
      real yt-dlp and a slow local HTTP server.

- [x] **2M3. Shutdown never stops in-flight work, so a redeploy ends in
      SIGKILL mid-write.** *Mechanism verified; file damage unverified.*
      `app/main.py:239-246`, `docker-compose.yml`. The lifespan cancels only
      the three loops; executor threads keep the process alive until Docker
      kills it after 10 s, possibly with rsgain or mutagen part-way through
      rewriting a library file.
      *Fix:* on shutdown set `stop_requested` on operations, cancel jobs
      (with 2M2's flag) and wait a bounded time; `stop_grace_period: 2m`.
      **Fixed:** the lifespan's shutdown now calls `_wind_down`: every
      running job is stopped the way Cancel does it (2M2: downloads stop,
      filing is waited for), and every running operation is asked to stop
      (`operations.stop_all`) and waited for up to 60 s. `docker-compose.yml`
      sets `stop_grace_period: 2m`. New test fails on the old code.
      **On the Pi:** its compose file is its own
      (`~/Docker/docker-compose.yml`), so `stop_grace_period: 2m` has to be
      added there by hand. An operation that does not check `stopping()`
      (combine, a cover apply) is waited for rather than stopped, which is
      what the grace period is for.

- [x] **2M4. A move into the library that dies part-way leaves a truncated
      track under the real name, and the next attempt files a second copy.**
      *Verified by simulation.* `app/filer.py:645-668`. `claim` creates the
      name, `shutil.move` copies onto it across filesystems; on failure the
      placeholder is removed only if still empty. The source stays in
      scratch or the inbox and is filed again as `(2)`, both carrying one
      track UUID. A kill between claim and move leaves a zero-byte file.
      *Fix:* copy to a hidden temp name in the target folder, then
      `os.replace` onto the claim; remove both on failure.
      **Fixed:** `_move_into_place` now brings the file into the target
      folder first, under a hidden `.<random>.part` name (a rename where it
      can, a copy across filesystems), and only then claims the real name
      and renames onto it in one step. A failure puts things back: a rename
      goes back to its source, a copy is dropped. The claim and the final
      rename are now microseconds apart, so the zero-byte placeholder a kill
      could leave is close to gone too. A `.part` left by a kill mid-copy is
      hidden from Navidrome. New test fails on the old code; the existing
      cross-device test now fakes EXDEV only across folders, as it is.

- [x] **2M5. One unreadable workspace marker stops the inbox for every
      account.** *Verified.* `app/workspace.py:209-249`,
      `app/inbox.py:415-427,483-491`, `app/main.py:258-267`. A root-owned or
      damaged `.owner` makes `existing()` raise out of `drain_all`; nothing
      is filed for anyone and only the log says so, every 15 s.
      *Fix:* try/except per workspace; record each loop's last success and
      last error and show them in Health.
      **Fixed:** `workspace.existing()` skips a directory whose marker
      cannot be read, logs it once rather than every pass, and counts it
      (`workspace.unreadable()`); `inbox.drain_all()` catches a drain that
      raises and records it on that workspace's `Result.broken`. New
      `app/heartbeat.py` keeps each background loop's last good pass, last
      failure and anything a good pass had to say; Health's System section
      shows one row per loop: FAIL when the last pass raised, WARN when it
      has a problem to report or has not completed for a while. The inbox
      row counts unreadable or broken workspaces without naming them, since
      Health is shown to every account. New tests fail on the old code.

- [x] **2M6. What the poller cannot file is still thrown away (M18 half
      fixed).** *Traced by reading; dot-folder case run.*
      `app/main.py:260-264`, `app/inbox.py:309-330,448-457`. `drain_all`
      returns failures but `_inbox_loop` reads only `changed`; only a
      browser upload's finish shows them. An SMB drop that cannot be filed
      is logged once and never mentioned. Never counted at all: formats not
      in `AUDIO_SUFFIXES` (`.aac`, `.wma`, `.aif`, `.dsf`, `.m4b`), dot
      folders, a file with a future mtime.
      *Fix:* keep the last result per workspace and show it on Health or the
      Drop page, listing what was not filed and why.
      **Fixed:** `inbox.drain_all` records each workspace's last pass:
      what is arriving, what failed and why, and, from the new
      `inbox.overlooked()`, what will never be filed (a format the filer
      cannot tag, anything under a hidden folder other than `.incomplete`,
      a file dated in the future). Health has a new per-person Inbox
      section, built from `inbox.status_for(username)` only, so nobody sees
      another account's file names: WARN "N not filed" naming the first
      five, INFO while files are arriving, FAIL when the inbox could not be
      read (2M5). This also covers 2H4's hidden-folder reporting. New tests
      fail on the old code.

### Album identity and covers

- [x] **2M7. A folder cover is still carried to the wrong album (H7's
      siblings).** *Verified.* `app/filer.py:516-550,724-726`, reached from
      `retag_track` and combine's loose tracks. Moving one track from an
      album with `cover.jpg` to one with embedded art only copies the old
      cover across, and Navidrome prefers it. A dropped folder holding
      several albums and one `cover.jpg` copies it into every album it
      feeds, including existing ones.
      *Fix:* carry a cover only when the whole folder is one album moving
      together; never from `retag_track`; skip if the destination already
      holds audio or any folder cover.
      **Fixed:** `file_track` takes `carry_cover`, and `retag_track` passes
      False. `_carry_cover` now copies only into a folder with no music and
      no folder cover of any name, and only when every other track in the
      source folder names the same album (it stops reading at the first
      that does not, so a hundred-album dump costs a few tag reads). New
      `filer.folder_cover()` finds a cover whatever its case; it is used
      here and is the helper 2M12 needs elsewhere. New tests fail on the
      old code. Behaviour change: a dropped album folder no longer brings
      its cover into an album that is already on disk.

- [x] **2M8. A registry miss mints a new album UUID without looking at
      disk.** *Verified.* `app/filer.py:387-392,707-710`. Files agree on an
      album UUID but their key is unregistered (retagged elsewhere, a
      backfill conflict). Moving a track into that album mints a fresh UUID,
      splitting it; the next *Save album* moves every file to the newcomer's
      UUID and the album loses its stars and plays.
      *Fix:* on a miss when moving, adopt the UUID the files already under
      that key or folder agree on (subject to 2H1's ownership check).
      **Fixed:** when `file_track` would otherwise mint (no UUID to adopt
      from the file, or a move) and the registry has never seen the key, it
      now reads the files already in the destination folder under that key
      (`_album_uuid_on_disk`). If they all carry one album UUID, that is
      recorded; if they disagree, or any carries none, it still mints rather
      than guess. 2H1's check still refuses a UUID another key owns. Applies
      to downloads and drops too, not only moves. New test fails on the old
      code.

- [x] **2M9. `album_key_of` reads only the first file, so an untagged stray
      that sorts first re-UUIDs the whole album.** *Verified.*
      `app/filer.py:436-451`, used at `:358` and `app/main.py:2138`.
      `require_one_album` ignores strays, but if the stray is first, `was`
      is empty and `after_retag` mints rather than moves. The old key stays
      registered against a UUID no file carries.
      *Fix:* take `was` from `_by_album`, the key the tagged files share.
      **Fixed** as suggested: `album_key_of` returns the one key the tagged
      files share, or "" when none name an album or (defensively) more than
      one do. Covers both callers, `retag_album` and *Use this*. New test
      fails on the old code.

- [x] **2M10. A rename that fails part-way cannot be finished, and the
      suggested route splits the album.** *Verified.*
      `app/filer.py:352-366,419-433`. If `write_tags` fails on file *k* (or
      the container dies), the folder holds two keys; retrying is refused as
      "more than one album", and editing track by track mints a new UUID for
      the remaining tracks.
      *Fix:* write an intent record before the first tag write and let
      `retag_album` resume a folder whose two keys are exactly that pair; or
      roll back the files already written.
      **Fixed, both ways.** `retag_album` saves the tags it is about to
      replace on each file and, when a write fails, puts them back on the
      files already written, so the folder keeps one name and a retry
      works. For what a rollback cannot cover (the container stopping
      mid-loop), it writes a hidden `.renaming` marker holding the old key
      before the first write and removes it once the files are filed; a
      folder holding the old key plus at most one other, with that marker,
      is treated as the rename in progress and finished from the old key.
      Without the marker two names are still refused. New tests; the
      rollback one fails on the old code.

- [x] **2M11. Health fails permanently once an artist has two album-less
      tracks.** *Verified.* `app/diskaudit.py:137-140`,
      `app/health.py:381-384`. Loose tracks are each their own record in
      `Artist/Unknown Album/` by design; the audit counts the folder as a
      split album.
      *Fix:* leave files with no album tag out of `by_directory`.
      **Fixed:** the audit's fast pass is unchanged; a directory that looks
      split is then re-read (`diskaudit._split`), and files that name no
      album are left out of the count. Only directories that already look
      split are re-read, so the cost is small. An unreadable file still
      counts. New test fails on the old code; a real split is still caught.

- [x] **2M12. Folder covers are recognised only in lower case.** *Verified
      for `covers._look`; the rest by reading.*
      `app/filer.py:512-540,325-327`,
      `app/covers.py:308-329,461-466,504-511`. On the Pi, `Folder.jpg` or
      `cover.JPG` is missed: *Fetch cover* embeds new art and reports
      success while Navidrome keeps showing the file; the survey judges the
      wrong picture; a dropped album's `Folder.jpg` is not carried.
      *Fix:* one helper that matches names and suffixes case-insensitively,
      used everywhere a folder cover is looked for.
      **Fixed** as suggested: `filer.folder_covers()` lists the folder once
      and matches by lower-cased name, in Navidrome's order. It now backs
      `filer.folder_cover`, `_carry_cover` (2M7), `leave_folder`,
      `covers.folder_covers` (so *Fetch cover*, *Square* and the survey see
      `Folder.jpg`) and `duplicates._leave`; the survey's `_look` matches its
      one listing the same way. New test fails on the old code even on
      Windows, which hides the bug in `is_file()` (it checks the names
      returned).

- [x] **2M13. `unfuse` gives the shared UUID to the keeper even when only a
      stray file carries it.** *Verified.* `app/unfuse.py:124-137,211-233`.
      Album A has 3 files on UUID `a`; album B has 9 on `b` and one stray on
      `a`. B is the keeper, so all 9 are rewritten to `a` and A gets a fresh
      one. The report shows `retired: []`.
      *Fix:* give the shared UUID to the album where it is the majority; add
      the keeper's dropped UUIDs to `retired`.
      **Fixed:** the keeper is now chosen only among albums whose majority
      is the shared UUID (by Navidrome's showing, then size, as before); if
      none, nobody keeps it. Every other album in the group keeps its own
      majority when no other album carries it, so only its stray files
      change, and gets a fresh UUID otherwise. The keeper's dropped UUIDs
      are now listed in `retired`. New test fails on the old code; the
      existing unfuse tests still pass.

- [x] **2M14. Resolving duplicates takes no folder lock.** *By reading.*
      `app/main.py:1327-1348,1429-1437`, `app/duplicates.py:566-633`. A
      resolve can move a file out of a folder that ReplayGain or a rename
      holds, leaving 2M10's state. The single resolve also skips the "still
      arriving" check. (The lock never covers a rename's or combine's
      destination either; see 2D3.)
      *Fix:* wrap each set-aside in `folderlock.holding(source.parent)` and
      report Busy per file; add `_not_arriving` to the single resolve.
      **Fixed** inside `duplicates.resolve`, so the single resolve and
      auto-resolve both get it: before anything is touched it refuses when
      a loser's folder is still arriving, then holds every loser's folder
      for the whole resolve, annotations included. Busy becomes a
      ValueError, which the single route reports as a 400 and auto-resolve
      as that group's failure. Nothing moves and no star is migrated when
      it refuses. New tests fail on the old code. The destination side of
      renames and combines (2D3) is still open.

### Listening

- [x] **2M15. A track the collector could not see at the first reading has
      its whole lifetime counted as plays when it appears.** *Verified;
      whether it has happened on the Pi is unknown.*
      `app/playcounts.py:179-200,617-642`, `app/store.py:168-176`. Only rows
      from the first reading are baselines. A track that was missing, set
      aside and restored, or not yet stamped has its full counter counted as
      new plays when it appears, dated at its last play (possibly months
      before collection began), and doubled with any Last.fm import. A
      variant: one of two files sharing a UUID goes missing and returns,
      adding the difference as phantom plays.
      *Check on the Pi:* rows after `play_collection.began` that are a
      track and user's earliest, with `play_count > 1` or
      `play_date < began`.
      *Fix:* treat a first-seen row as a baseline when its `play_date`
      predates the previous reading; never count a rise older than the
      reading before it.
      **Fixed** with a narrower rule than suggested: a rise whose latest
      play (`play_date`) is before collection began is treated as a
      baseline, never as plays. That covers both the reappearing track and
      the returning duplicate file. "Older than the previous reading" was
      not used: a client syncing offline plays late stamps them in the
      past, and those are real. Applied when plays are computed from the
      stored readings, not when they are taken, so any phantom plays
      already in the Pi's history go away on deploy without touching
      state.db. A first row with no `play_date` still counts, as before.
      New test fails on the old code.

- [x] **2M16. The Last.fm fetch double-counts and can silently truncate.**
      *Verified with a mocked `_call`.* `app/lastfm.py` (`scrobbles`). Pages
      ran newest first with no `to=`, so a scrobble landing mid-fetch
      returned an old one twice. `totalPages` was re-read from every page,
      so an empty answer ended the fetch as complete. A single-track page
      raised `AttributeError`.
      **Fixed** (`b27735b`): `to=` anchored at the start, dedup on
      `(artist, track, uts)`, page count taken from page 1, an empty page
      part-way raises `LastfmError`, a single object is read as a list.
      Temporary errors are still not retried: see 2L19.

- [x] **2M17. `library_index` counts missing files, so a resolved duplicate
      makes its scrobbles "ambiguous".** *Verified.*
      `app/lastfm.py:200-212`. Navidrome keeps a set-aside copy's row as
      missing; with two UUIDs per title, every scrobble of that track is
      dropped from the import. Likely the source of the 3,402 scrobbles
      assigned by hand.
      *Fix:* build the index from live rows (`navidrome.live_clause`),
      falling back to missing rows only where a key has no live candidate.
      **Fixed** as suggested: the query marks each row live or not with
      `navidrome.live_clause`, and a key's candidates are its live UUIDs,
      or its missing ones only when it has no live file. New test fails on
      the old code. The scrobbles already assigned by hand are untouched;
      a re-import would now match them itself.

- [x] **2M18. A second plain Last.fm import adds rows instead of replacing
      them when a match has moved.** *Verified.* `app/lastfm.py:341-354`,
      guard at `:638-642`. Re-run after a duplicate resolve or a retitle and
      both the old-UUID and new-UUID rows remain: 4 plays for 2.
      *Fix:* in one transaction, delete this user's and source's bare-date
      rows before inserting the new plan.
      **Fixed, more cautiously than suggested.** Deleting every earlier row
      would also delete rows assigned by hand, which look exactly like rows
      this plan cannot reproduce. So `write` now finds the earlier day rows
      the new plan does not reproduce and refuses (`StaleRows`), writing
      nothing, unless `--replace` is given, which drops them in the same
      transaction. It also rolls back on any failure now. On the Pi this
      path is already refused while `--times` rows exist (M2). New test
      fails on the old code; running the same import twice still changes
      nothing.

- [x] **2M19. The smart playlist editor drops `order` (and `offset`) on
      save.** *Verified.* `app/playlists.py` (`to_form`, `to_rules`). A rule
      written as `"sort": "playcount", "order": "desc"` opened as ascending,
      and Save turned the 50 most played into the 50 least played.
      `offset` and `limitPercent` vanished; a multi-field sort opened as
      editable and failed on Save.
      **Fixed** (`943b8b2`): `order` is read (a `-` and `order: desc` cancel
      out); `offset`, `limitPercent` and an unknown sort raise
      `Unsupported`. Such playlists now open as not editable here.

- [x] **2M20. M29's remainder: no writer rolls back, so a failed write is
      committed half-done by the next one.** *Verified.* `app/store.py`.
      Still unlocked on the shared connection: `playcounts.baseline_stamp`,
      `_last_known`, `last_reading`, `status`, `history_version`,
      `_plays_between`, `coverage`, `_person_coverage`, `overview.warm`,
      `health._decisions_version`. Dirty reads are harmless today. The real
      hazard: with `_take`'s run-log insert forced to fail, the connection
      stayed in a transaction, unlocked readers served uncommitted rows, a
      CLI writer got `database is locked`, and the next unrelated
      `mark_reviewed()` committed the half-reading. `registry.repoint`
      (DELETE then INSERT or UPDATE) could be half-committed the same way.
      The CLI tools are second writers that the per-process lock does not
      cover.
      *Fix:* one `store.transaction()` context manager that takes the lock
      and rolls back on exception, used by every writer; take the lock in
      the reads above.
      **Fixed.** `store.transaction()` holds the lock for a write, commits
      when the block ends and rolls back when it raises; nested blocks join
      the outermost. Every writer uses it: store's four, the registry's four,
      a play-count reading and the Last.fm import (`write_times` already
      managed its own). The reads are covered in one place:
      `store.connection()` now returns a wrapper that runs each statement
      and fetches its rows under the lock, so every reader, including any
      written later, waits for a write in flight. The lock is now an
      `RLock`, so a locked read inside a transaction cannot deadlock.
      Reproduced first with the run-log table dropped mid-reading; the new
      test fails on the old code. Still open from M29: the CLI tools are
      separate processes the lock cannot cover (they get `database is
      locked` and a 5 s wait, which is logged).

### Frontend

- [x] **2M21. The Library tabs race each other; M5's counter covers only
      albums against albums.** *Verified in Chromium, all four directions.*
      `app/static/js/library.js:304-345,423-439`,
      `app/static/js/library-attention.js:68-72,151-223`. A slow albums
      answer draws the grid under Artists or Needs attention, and the
      reverse.
      *Fix:* one request token owned by `loadLibrary`, checked by all three
      loaders.
      **Fixed:** the counter is now `libraryState.load`, shared. The albums,
      artists and attention loaders each take the next number when they
      start, including `loadAttention` when it is called directly, and draw
      only if no load has started since. Verified in Chromium with the
      same four scenarios that showed the bug (all four BUG on the old code,
      all four clean now); a static test pins the wiring.

- [x] **2M22. A session that ends while the socket stays open never shows
      the sign-in form.** *Verified.* `app/static/js/main.js:129-136`,
      `app/static/js/ws.js:87`. Only socket close 4401 leads to sign-in; no
      JS looks at HTTP 401. After the 30-day cap, a removed account or a
      sign-out in another tab, every panel says "Please sign in." until a
      reload.
      *Fix:* a shared fetch wrapper that handles 401 (see 2D5); close that
      user's sockets with 4401 in `auth.sign_out`.
      **Fixed, both halves.** `core.js` has `apiFetch`, which calls the
      handler main.js registers (`whenSignedOut`) on any 401; every API call
      outside the sign-in flow now goes through it (30 call sites in 12
      modules). The handler runs once however many requests fail at once,
      and closes the socket; `ws.js`'s `disconnect()` no longer triggers a
      reconnect. Server side, the broker records which session opened each
      socket, and signing out closes that session's sockets, and only that
      session's, with 4401. Verified in Chromium (S7 now shows the sign-in
      form); new tests fail on the old code. This is the first piece of
      2D5's shared fetch layer.

- [x] **2M23. Combine and *Use this* treat "not started" as success.**
      *Verified for combine; Use this by reading.*
      `app/static/js/library-combine.js:356-374`,
      `app/static/js/library-drawer.js:763-778`,
      `app/static/js/operations.js:35-40`. With one combine running, a
      second answers `started: false`; the dialog closes, the selection is
      cleared, and the second never runs. *Use this* closes the drawer the
      same way.
      *Fix:* treat `!payload.started` as a refusal; keep the dialog and
      selection and say why.
      **Fixed** as suggested. Combine keeps the dialog and the selection and
      says in the dialog that another combine is running. *Use this* now
      starts the retag before closing anything; when it is refused or not
      started, the drawer stays open and the reason appears in the
      candidates list, since the panel's status line is behind the drawer.
      Verified in Chromium (S4, and a new S14 for *Use this*; both BUG on
      the old code); a static test pins the wiring. Changing what
      `startOperation` returns is left to 2D5.

- [x] **2M24. A drop is split across two libraries if the Into picker
      changes mid-upload.** *Verified.* A gap in L11.
      `app/static/js/drop.js:116-117,140-143`. `targetLibrary()` is read per
      file and again at finish.
      *Fix:* capture it once per batch.
      **Fixed** as suggested: `dropQueueBatch` reads the picker once, when
      the drop is queued, and every upload and the finish of that drop use
      it. Verified in Chromium: S10 now sends `1, 1, 1, finish:1`.

- [x] **2M25. Escape in a text box discards the selection or closes the
      album panel.** *Verified.* `app/static/js/library.js:628-633`. Escape
      in the search box exits select mode and wipes the selection; in the
      "Merge into" box it closes the unsaved editor. `nav.js:66` handles
      Escape too, unaware of this one.
      *Fix:* ignore Escape from inputs, selects and textareas, and while
      the menu is open.
      **Fixed:** `core.js`'s `pageEscape(event)` says whether Escape is the
      page's: not when a text box (or select, textarea, contenteditable) has
      it, and not when something has already used it. The Library, Browse
      and Downloads handlers all use it. The menu's handler now runs in the
      capture phase and marks the key used, so closing the menu leaves the
      panel behind it open. Verified in Chromium: S3, S3b and a new S15 for
      the menu, all BUG on the old code. Side effect: Escape typed in one of
      the combine dialog's fields no longer closes the dialog; Escape
      elsewhere in it still does.

- [x] **2M26. A refused inline track edit leaves the unsaved text in the
      field.** *Verified for the page; frequency on the Pi unverified.*
      `app/static/js/library-drawer.js:375-407,447-462`. Save-on-blur meets
      M14's non-blocking lock: a second edit during the first gets 409, the
      text stays, the next save says "Saved.", and nothing retries.
      *Fix:* restore the old value on failure; chain one album's edits so
      they never overlap.
      **Fixed** as suggested: a failed save puts the field back to the value
      on the file (the warning says why), and inline track saves go through
      one promise chain, so each waits for the one before. Verified in
      Chromium: S9 (field restored) and a new S16 (the second of two quick
      edits is sent after the first answers, where it used to go 10 ms
      later); both BUG on the old code.

- [x] **2M27. A client the broker drops for a stalled send stays connected
      and deaf.** *Verified with a fake socket.* `app/main.py:175-191`.
      After the 5-second timeout the socket is unregistered but never
      closed, so the page shows "live" and receives nothing.
      *Fix:* close it (code 1011, bounded) when dropping it.
      **Fixed** as suggested: after unregistering a stalled socket the
      broker closes it with 1011, bounded by the send timeout and in the
      background so a dead peer holds nothing up (the task is kept until it
      finishes). The page sees an ordinary close and reconnects for a fresh
      snapshot. New test fails on the old code.

- [x] **2M28. Bulk *Square covers* caches the old picture for a week.**
      *Verified in Chromium; unverified against Navidrome.*
      `app/static/js/library-attention.js:306-317`,
      `app/static/js/library-shared.js:127,140-141`, `app/main.py:1956`.
      `applyCover` waits 20 s before stamping the art URL; `squareCovers`
      stamps at once, so the stamped URL caches the old art
      (`max-age=604800`). `artStamps` lives in memory, so after any cover
      change a reload shows the old art for up to a week.
      *Fix:* the same wait; better, a cover version (file mtime) from the
      listing as `v=`.
      **Fixed, both.** The listing now gives each album an `art_version`:
      the art track's `updated_at` in Navidrome, which changes once
      Navidrome has rescanned the rewritten file. It is part of the art URL,
      so a new cover is fetched afresh after a reload too (a file mtime was
      not used: it changes before Navidrome serves the new art, which is
      the bug). `squareCovers` now stamps after the same rescan wait as a
      single cover; the constant moved to `library-shared.js`. New server
      test fails on the old code; in Chromium, S11 no longer sees a stamped
      fetch straight after squaring.

- [x] **2M29. The Combine dialog rebuilds itself under the user.**
      *Verified.* `app/static/js/library-combine.js:120-129,264-269,336`.
      Spotify's guess arriving mid-typing replaces the input and loses
      keystrokes; every keyboard ↑/↓ drops focus to the page; the dialog
      takes no focus on open and has no trap.
      *Fix:* update the hint and summary in place; re-focus the moved row's
      button; focus the sheet on open.
      **Fixed** by making the rebuild keep focus, rather than splitting the
      render: every control in the sheet carries a stable `data-focus` key
      (the move buttons keyed by track path, not position), and after a
      rebuild the same control gets the focus and its caret back. A track
      moved to the end falls back to its other button. The sheet takes
      focus when it opens, and Tab is kept inside the dialog. Verified in
      Chromium: S5, S5b and a new S17 (focus on open, Tab trap), all BUG on
      the old code.

## Low

### Getting music in and the platform

- [x] **2L1. Two Retry requests at once start two runners on one job.**
      *Verified.* `app/main.py:859-882`. `RUNNING` is checked before an
      await and set after it. The page's Retry button can also be pressed
      twice, because the track list is rebuilt on every job message
      (`downloads.js:194-210`). *Fix:* reserve the slot before the await.
      **Fixed, both sides.** `retry_job` checks `RUNNING` again after its
      one await, and takes the slot before the next one (`push_job`). The
      page keeps a set of jobs whose Retry is in flight, so a rebuilt button
      comes back disabled and a second press is ignored. New server test
      fails on the old code.
- [x] **2L2. A cancel just after resolving leaves a job stuck at "queued".**
      *Verified.* `app/main.py:637-657`. `push_job` is awaited outside any
      handler. *Fix:* one try/except/finally around `_resolve_job`'s body.
      **Fixed** as suggested: one `CancelledError` handler now covers the
      whole of `_resolve_job` before the run starts, and a job that had
      already failed keeps its reason. Found alongside: refusing a link
      over `MAX_TRACKS_PER_JOB` returned without clearing `RUNNING`, so
      Retry said "still running" for the life of the process; every path
      that will not run now clears it. Both new tests fail on the old code.
- [x] **2L3. A Settings save that fails still changes the live settings.**
      *Verified.* `app/config.py:204-214`. Invalid TOML or a read-only
      `/config` gives a 500 with the change in force until restart. A
      hand-written TOML table is rewritten as a string.
      *Fix:* write from a copy; apply live only after `os.replace`.
      **Fixed, both.** `config.save` builds the file from the changes
      without touching the live settings, and applies them only after
      `os.replace`. The serialiser now writes tables (inline), arrays and
      dates, and quotes keys that need it, so a hand-written table comes
      back as a table. Both new tests fail on the old code.
- [x] **2L4. The upload size check runs after the body is stored.**
      `app/main.py:954-958`. Starlette spools the multipart body to `/tmp`
      first; one request can fill the SD card.
      *Fix:* refuse on `Content-Length` in the middleware.
      **Fixed** as suggested: the middleware checks an upload's declared
      length before the handler, and so before any of the body is read:
      413 above the limit plus 1 MB of form framing, 411 with no length (a
      browser always sends one for a form). The handler's own checks stay
      as a second line. New tests fail on the old code.
- [x] **2L5. Some errors give a bare 500.** `resolve_duplicate` and
      `dismiss_duplicate` (`app/main.py:1332,1358`) do not catch
      `navidrome.Unavailable`. `Workspace.prepare()` raises when two
      usernames reduce to one slug (`app/workspace.py:137`) and is called
      unguarded from `create_job` and `upload_to_inbox`.
      **Fixed:** resolve and dismiss read the groups through `_groups_for`,
      which turns `Unavailable` into a 503 with its message, as the list
      route already did; both callers prepare the workspace through
      `_prepare`, which turns the refusal into a 409 with its message. New
      tests fail on the old code.
- [x] **2L6. Sessions outlive a password change, and an open `/ws` outlives
      sign-out.** A Navidrome password change does not end sessions here
      (up to 30 days); a socket keeps receiving events until it disconnects.
      **Fixed.** A session records a fingerprint of the password Navidrome
      stores (`navidrome.password_mark`, a hash, never the value) at sign-in;
      the periodic account re-read compares it and signs the session out
      when it changed. "Cannot tell" (no column, unreadable) is never read
      as a change. Sign-out already closes the session's sockets (2M22); an
      open socket now also asks every minute whether its session still
      exists and closes with 4401 when it does not, covering expiry and a
      removed account. New tests fail on the old code. Caveat: if Navidrome
      ever re-encrypts stored passwords (a key change), everyone is signed
      out once.
- [x] **2L7. yt-dlp's warnings are discarded and its age is invisible.**
      *Unverified.* `app/downloader.py:23-37`, `requirements.txt`. Warnings
      go to `log.debug`; the pin moves only with a commit; the image has no
      JavaScript runtime, which recent yt-dlp wants for full YouTube
      support. *Fix:* log warnings at WARNING (deduplicated), show the
      version in Health, add Dependabot or a scheduled rebuild.
      **Fixed, all three.** yt-dlp's warnings are logged at WARNING, each
      message once per process. Health's System section has a yt-dlp row
      with the version and its age, read off the version (they are dates),
      and WARN past 60 days. `.github/dependabot.yml` opens a weekly pull
      request for new pip releases, which rebuilds the image once merged.
      New tests fail on the old code. Not done: installing a JavaScript
      runtime in the image for yt-dlp's full YouTube support. That changes
      the image's size on the Pi and wants a decision first.
- [x] **2L8. A direct-link album splits on guest tracks and loses its
      order (M22 incomplete).** *Verified on the item shape only.*
      `app/generic.py:95-122`. `album_artist` is each track's own artist and
      `track_no` is always `None`; the comment at `:118-119` is stale.
      *Fix:* first of `artists` (or `album_artist`) for the album artist,
      `track_number or playlist_index` for the number.
      **Fixed.** Each item takes the site's album artist where it gives one,
      else the first credited artist, and the site's track number where it
      gives one. A playlist whose entries all name one album is then treated
      as that album (`_as_album`): one album artist (the most common), the
      album's length as its total, and the playlist's order as the track
      numbers when the site gave none. A list of unrelated videos is left
      as singles. The stale comment is gone. New test fails on the old code;
      tried on the item shape only, not on live yt-dlp output.
- [x] **2L9. A numbered duplicate is renamed on every save of its album.**
      *Verified.* `app/filer.py:628-642,717-727`. `Same (2).mp3` goes to
      `(3)`, then back. *Fix:* leave a file that is already one of its
      target's numbered variants in the same folder.
      **Fixed** as suggested (`filer._already_there`), and the file's own
      path is what `file_track` reports when it stays. New test fails on
      the old code.
- [x] **2L10. Unbounded dicts.** `_offered` (`app/main.py:2089`) and
      `_sign_in_failures` (`app/main.py:517`) are never pruned.
      **Fixed:** each sign-in check now drops every address's failures
      older than the window, not only the asking address's. Offered matches
      are recorded through `_offer`, which keeps when each list was made and
      drops lists older than a day. New tests fail on the old code.
- [ ] **2L11. Transitive dependencies float.** Only top-level requirements
      are pinned and the base image is not pinned by digest. Dev runs
      Python 3.14 against 3.13 in the image.

### Library and listening

- [ ] **2L12. Album edit, combine and *Use this* report success when the
      identity write failed; combine aborts on an `OSError`.**
      `app/filer.py:499-504`, `app/main.py:1888-1898,2157`,
      `app/combine.py:93-111`. *Fix:* return `identified` from each and show
      it; catch `OSError` per album and per track in combine.
- [ ] **2L13. A quarantined file put back by hand stays invisible to the
      duplicate finder.** `restored_at` is read in three places and written
      nowhere (`app/store.py:362-374`, `app/duplicates.py:253-255,700`).
      *Fix:* filter on whether the quarantined file still exists; stamp
      `restored_at` when it is found back at its source.
- [ ] **2L14. `tools/fix_broken_m4a.py --all --mode mp3|flac` can overwrite
      an existing file of the same stem.** `:216-219`. It also still walks
      the quarantine (`:88`). *Fix:* refuse or number the name; skip
      `duplicates-removed/`.
- [ ] **2L15. Plays on tracks with no UUID are dropped every reading and
      reported nowhere.** `app/playcounts.py:198-200,344-353`.
      `without_uuid` appears only in the forced snapshot's JSON. *Fix:*
      store it in the run log, log it, show it in coverage.
- [ ] **2L16. `/api/playcounts/top` accepts unpadded dates and returns an
      empty range.** *Verified.* `app/main.py:2491-2499`. *Fix:* pass the
      parsed dates on, reformatted.
- [ ] **2L17. Home shows an all-zero history when the history read fails.**
      `app/overview.py:522-530`. *Fix:* `"available": false` and a banner.
- [ ] **2L18. The `duplicate_dismissed` migration is not atomic.**
      *Verified.* `app/store.py:231-248`. A crash between steps strands the
      old table, and every "keep both" returns to the review list. *Fix:*
      explicit `BEGIN`/`COMMIT`; copy from `_old` whenever it exists.
- [ ] **2L19. Last.fm temporary errors are not retried.** Body errors 8,
      16 and 29 and HTTP 429 throw away the whole fetch (the rest of 2M16).

### Frontend

- [ ] **2L20. Duplicates shows failures as "nothing there".** *Verified.*
      `app/static/js/duplicates.js:219-270`. A 503, 401 or 500 on the
      preview reads "Nothing is confident enough"; a 500 on the set-aside
      list reads "Nothing has been set aside"; the button re-enables while
      its operation runs.
- [ ] **2L21. The Library's caches survive a change of account.**
      *Verified.* `app/static/js/main.js:90-92,129-136`,
      `library-shared.js:155-183`. After a 4401, a second person sees the
      first's Artists list. *Fix:* reload, or reset `libraryState` and
      `selection` on expiry.
- [ ] **2L22. The Artists list is refreshed only by Rescan and combine.**
      `library.js:427,684`, `library-combine.js:390`. *Fix:* drop it in
      `refreshLibrary()`.
- [ ] **2L23. Quarantine on an album opened from song search throws.**
      *Verified.* `library-drawer.js:798-800`. `album.tracks` is undefined
      until loaded; M8 guarded Edit details only.
- [ ] **2L24. The selection goes stale after the thing selected moves.**
      `library-drawer.js:393-401,491-496,811-813`. A renamed or quarantined
      album stays selected under its old folder; a renamed track keeps its
      old key.
- [ ] **2L25. A cover survey in flight can undo an invalidation.**
      `library-shared.js:192-211`. *Fix:* a generation counter.
- [ ] **2L26. Silent failures when the server is unreachable.** Sign-in and
      Settings load and save have no `catch` (`main.js:75-95`,
      `settings.js:19-21,69`).
- [ ] **2L27. One hung upload stalls every later drop.** `drop.js:123-133`.
      `JSON.parse` can throw in `onload`, and there is no `onabort` or
      `ontimeout`, so `dropChain` never advances.

## Design

- [ ] **2D1. A job's lifetime is its asyncio task, but the work lives in
      threads and subprocesses nobody tracks.** "Stopped", the gate count
      and `RUNNING` describe the task. 2H2, 2M2 and 2M3 all follow from
      this; fixing it is the root fix for them.
- [ ] **2D2. Three stores of album identity, updated in sequence, with
      nothing reconciling them.** The registry, the tags on disk and
      Navidrome's database. When registry and disk disagree, the registry
      silently wins on the next edit (verified: *Save album* rewrote every
      file to the registry's UUID). `album_registry` has no uniqueness on
      `(library_id, album_uuid)`. 2H1, 2M8, 2M9 and 2M10 are forms of this.
      *Direction:* a registry-against-disk comparison in the disk audit,
      shown in Health; one "disk wins unless ambiguous" function used by
      `file_track` and `after_retag`; 2M10's intent record.
- [ ] **2D3. The folder lock covers where files leave from, never where
      they arrive.** A rename into an existing album, a combine's target and
      all inbox filing are unlocked; `inbox.receiving` is checked once, not
      held.
- [ ] **2D4. Business logic lives in route handlers.** About two thirds of
      `main.py`: `library_match_apply.run`, `library_combine`,
      `library_cover_apply.run`, the quarantine handlers and the job
      lifecycle. During R1, move the `run()` bodies into the domain
      modules. There are no import cycles, but `replaygain` imports
      `operations`, `library`, `inbox` and `folderlock`, and `library`
      imports `playcounts`. All in-process state assumes one uvicorn
      worker, and nothing enforces that.
- [ ] **2D5. The frontend has no shared fetch layer.** 30 raw `fetch` calls
      against 19 through `getJSON`/`postJSON`, with three error
      conventions; the root of 2M22, 2L20 and 2L26. `startOperation`
      returns three shapes that callers decode by hand (2M23). "Latest
      request wins" is written four times and missing where it matters
      (2M21). Cache invalidation is spread over four modules (2L22, 2L25).
      Duplicated helpers have drifted: `plural` three times, two album
      drawers, four Escape handlers (2M25). `call()` in `core.js` is dead.
- [ ] **2D6. Three definitions of "a track in the library" already
      disagree.** `playcounts._current` hard-codes the missing flags,
      `navidrome.live_clause` probes the schema, and `lastfm.library_index`
      has no filter (2M17). `UUID_TAG` is defined twice. `history_version`
      cannot see an in-place `UPDATE`.

### R1 advice

The proposed split is right in outline, with four changes: add `events.py`
for the broker and the `push_*` functions; make `space_for` a plain async
helper, not a dependency (callers take `library_id` from four places, and
some fold checks into one thread hop); add router-level
`Depends(current_session)` on top of the middleware, not instead of it; and
do not let other routers import `JOBS` or `RUNNING`.

The hazard is the tests. About 70 `main.X` names are patched across 18 test
files. Once a function moves, a re-export from `main` keeps the import
working but not the patch: an ineffective stub of `_run` or `_resolve_job`
in `test_jobs.py` means real resolving and downloading. Patches of the form
`main.workspace.for_session` keep working only if the new modules call
module-qualified (`workspace.for_session(...)`), so make that a rule.

Order:

1. Fix 2L1 and 2L2, which are local to code about to move.
2. Add a characterisation test of every route's method, path and guard,
   plus "every route answers 401 without a session".
3. Extract `events.py`, `jobs.py` and `background.py`, one commit each,
   moving each test's patch targets with the code.
4. Add `api/deps.py`.
5. Move the routers leaf-first, `library_edit` last.
6. Add the router-level dependencies and re-run the test from step 2.
7. Only then push the `run()` bodies into the domain modules.

## Round 2 test gaps

- **Still no HTTP-level tests.** Handlers are called as functions, so
  `Depends`, the middleware and route registration are never exercised; a
  dropped guard would pass the suite.
- **The cancellation tests use `asyncio.sleep` for the download**, so they
  cannot see threads that outlive the task (2M2).
- **`tests/conftest.py`'s `media_file` has no `updated_at`**, so every
  track-index test runs on the file-stamp fallback, not the production path.
- **`tests/test_frontend.py` is regex only.** The Playwright stub-route
  checks that verified the frontend findings live in scratch folders; they
  could run in CI as a dev dependency.
