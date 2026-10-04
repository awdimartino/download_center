# Code review — 2026-10-04

A read-only review of the whole codebase (Session 11), at commit `6e45ae2`.
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

- [ ] **M29. `state.db`'s shared connection is read without the lock.**
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
      nobody has seen, so it waits for evidence.

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
- [ ] **L19.** `audio_bitrate = 320` re-encodes YouTube's ~130–160 kbps Opus
      at twice the size for no gain. Consider 192, or keeping Opus/M4A.

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

- [ ] **L32.** `/docs`, `/redoc` and `/openapi.json` are open without
      sign-in (`main.py:354`).
- [ ] **L33.** The cookie's 14-day `max_age` is set once at sign-in while the
      server session slides; active users are signed out on day 14
      (`main.py:462-466`).
- [ ] **L34.** `tools/` is not in the image, but `tools/fingerprint.py`'s
      usage says to run it there; it also writes the MusicBrainz recording
      id into the *AcoustID Id* frame (`fingerprint.py:193`), never
      checkpoints failures, and walks the quarantine.
- [ ] **L35.** Settings has no `beets_enabled` control, and a non-secret
      field (Navidrome URL, Spotify id) can never be cleared
      (`index.html:466-476`, `settings.js:42`).
- [ ] **L36.** CLI tools run as root under `docker exec`, leaving root-owned
      files in `/config`. *Fix:* warn or refuse when `geteuid() == 0`.
- [ ] **L37.** Updating a playlist when Navidrome is unreachable is a 500,
      not a 502 (`main.py:1270`).
- [ ] **L38.** CSRF rests on `SameSite=Lax` alone, which ignores ports — a
      page on another port of the same host (Navidrome, Calibre) could fire
      body-less POSTs: `/api/duplicates/auto?apply=true`,
      `/api/health/audit`, `/api/library/rescan`, `/api/playcounts/snapshot`.
      `/ws` checks no Origin. *Unverified in practice.* *Fix:* reject
      non-GET requests whose `Origin`/`Sec-Fetch-Site` is not same-origin.
- [ ] **L39.** Sign-in returns raw exception text (internal hostnames) to
      unauthenticated callers and is not rate-limited (`main.py:454-457`).
- [ ] **L40.** Cover art is served `Cache-Control: public` though it is
      owner-checked (`main.py:1692`); use `private`.
- [ ] **L41.** Blocking filesystem and database work runs on the event loop
      inside `async` routes and the auth middleware (`_library_roots`,
      `album_dir`, `audio_in`, `settled`, `auth.py:130-137`).
- [ ] **L42.** Arbitrary server-side fetches: direct links and thumbnails
      reach any URL a signed-in user supplies, including LAN addresses, with
      the first 200 characters of errors returned; `choosable()` is checked
      before redirects only.
- [ ] **L43.** `with sqlite3.connect()` does not close connections (it only
      commits); use `contextlib.closing`.
- [ ] **L44.** `audio_bitrate` is unvalidated text and `rate_limit_sleep`
      unbounded (`config.py:48-51`).
- [ ] **L45.** Focus rings are `box-shadow`, which vanish in forced-colours
      mode; there is no `forced-colors` rule (`style.css`).

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
- [ ] **R2. Split `app/static/js/library.js` (~2,300 lines)** along its own
      sections: list, attention, drawer and editing, combine. Pass options to
      `showProgress` instead of special-casing the string "ReplayGain"; share
      the duplicated cover-survey fetch; use the existing `button()` helper in
      `renderBar`.
- [ ] **R3. Stale text describing the removed staging, ledger, nightly and
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
- [ ] **R4. Dead code:** `playcounts.last_complete_day` and `taken_on()`,
      `registry.album_uuid_for` (tests only), the `filing` item status (only
      the front end knows it — set it before `deliver`, or drop it).
- [ ] **R5. Duplication:** `_download_with_retries` and
      `_match_with_retries` share one backoff loop (`worker.py:68-117`);
      `create_job` still writes a beets config even when beets is disabled
      (`main.py:614-617`); `Workspace.prepare` makes `beets_dir` on every
      15-second poll; `health.py:536-537` attaches a row to `sections[1]` by
      position; the stamped count is computed twice (`health.py:114, 518`);
      `main.py:1967-1968` double-counts tracks inside selected folders.

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
