# Download Center — how the code works

A reference for the codebase as it stands on 2026-09-25, after the
direct-to-library redesign. For what it is *for* and where it is going, see
[PLAN.md](PLAN.md); to pick up work, see [HANDOFF.md](HANDOFF.md).

**Step 6 of that redesign is not done** - the one cleanup pass over Alex's
three piles of music. Step 5 is: `staging.py`, `stamp.py`, the download
ledger, the nightly sweep, `tools/ensure_uuid.py` and beets' auto-import path
are gone from the tree, about 2,900 lines of it.

---

## Shape

One FastAPI process serving a JSON API, a WebSocket event stream, and a
static single-page front end. It runs as one container on a Raspberry Pi,
beside a Navidrome container, sharing the music volumes and Navidrome's
database file.

```
browser  ──HTTP/WS──▶  download_center  ──read-only sqlite──▶  navidrome.db
                              │
                              ├──HTTP (native + Subsonic API)──▶  Navidrome
                              ├──subprocess──▶  yt-dlp, ffmpeg, beets
                              └──filesystem──▶  inbox/ ─▶ music/, kelly/
```

The front end is three files, no build step and no framework:
`app/static/index.html`, `app.js`, `style.css`. They are served by the same
process, so a deploy is a container restart and a browser refresh.

---

## The four places state lives

Knowing which store owns a fact is most of understanding this codebase.

| Store | Owns | Access |
|---|---|---|
| `navidrome.db` | Accounts, libraries, stars, ratings, play counts, the scanned index | **Read-only.** Every write goes through Navidrome's API |
| `config/state.db` | Which album UUID an album key maps to; listening history; duplicate decisions | Read/write, ours alone. `store.py` owns the connection |
| `config/beets/<workspace>/library.db` | Beets' index of one person's filed music | One per workspace |
| The audio files | `navidrome_uuid`, MusicBrainz ids, all tags | The truth. Everything else is a cache |

`state.db`'s most important table is now `album_registry`
(**library_id**, **album_key**, album_uuid, created_at). It is the answer to
"which album is this track on", and it replaced inferring that from directory
layout plus a majority vote among neighbours - see *Identity* below.

The **download ledger is gone** (2026-09-25). It recorded that a track had
been fetched, which stayed true after the file was deleted, replaced or moved
- so a track that left the library became permanently unfetchable, reported
as "skipped" with nothing to say why. Browse's "in library" marker asks
Navidrome what the library holds instead, which is both the right question
and a broader answer: a CD rip was never in the ledger either. Nothing
refuses a download now. An existing `ledger` table is left on disk rather
than dropped - destroying somebody's rows on an upgrade is not the code's
decision to make.

`duplicate_dismissed` (group_key, note, decided_at) holds
"keep both" decisions. `duplicate_quarantined` records every file the
duplicates flow set aside — source, target, keeper, who decided — because
"nothing is deleted" is only useful if the file can be found again.

`play_snapshot` and `play_anomaly` are the play-count record. Navidrome
keeps a cumulative count and only the latest date, so history it has already
overwritten is unrecoverable - these are the only copy. Keyed by track UUID
rather than `media_file.id`, and only *changed* counts are stored, so a year
is tens of thousands of rows rather than near a million.

Read every five minutes, not nightly. Navidrome stores the moment of a
track's *most recent* play beside its running total, so a reading that
catches the count rising by one carries that play's exact timestamp - and at
this cadence the rise is almost always one. That is what turns a running
total into a log of individual plays, and it is the difference between
"played 1,204 times" and being able to answer anything about sessions, time
of day, or a particular evening. The cost is one indexed join over a few
thousand `annotation` rows: 34ms on the Pi, about ten seconds of work a day.

`taken_on` therefore holds a timestamp. Rows written while this ran nightly
hold a bare `YYYY-MM-DD`, and the two coexist in the one column with no
migration: a date is a prefix of every timestamp on the same day, so it
sorts first and still sorts before the next day. The consequence for queries
is that a day is bounded half-open - `taken_on < next_day(D)`, never
`taken_on <= D`, which would exclude every reading actually taken on D.

`play_imported` holds listening from before the snapshots began - alex's
Last.fm history, imported once in September 2026 and matched to tracks by
artist and title. Kept apart from `play_snapshot` because the two are
different kinds of evidence: a snapshot is a cumulative total this app read
itself, an import is somebody else's record matched by text.
`playcounts.plays_between` reads both, and the two cover disjoint periods by
construction - the import stops the day snapshots start.

The tool that produced it is deliberately not in the tree. It ran once.

It deliberately holds no user table, no library table and no copy of
anything Navidrome knows. `library_id` is a foreign key in spirit only:
Navidrome owns what a library *is*.

---

## Identity: the UUID

The single most important idea here, and the reason for a multi-day
migration.

Navidrome derives a track's persistent id from a configurable chain of
tags. This library is configured as:

```
PID.Track = 'navidrome_uuid|musicbrainz_trackid|albumid,discnumber,tracknumber,title'
```

Every file carries a `navidrome_uuid`. Because it is first in the chain,
a track's id survives re-tagging, moving, renaming and a rebuilt database —
which is what makes stars, ratings and play history durable.

**The trap:** Navidrome's `computePID` hashes the *empty string* when a tag
is missing. A chain of a single field would therefore give every untagged
file the same id, silently collapsing them into one track. Never configure
a bare single-field PID; always keep fallbacks after it.

Reading and writing these tags is `uuidtags.py`. On MP4/M4A the freeform
atom **must** use the `com.apple.iTunes` mean — TagLib, and therefore
Navidrome, does not expose a custom namespace. An earlier version wrote
`----:com.navidrome:UUID`, which was invisible to Navidrome; the reader
still accepts that legacy form.

---

## Per-user model

There is no user table. `auth.py` signs in against Navidrome and holds the
answer in memory for a fortnight; a restart signs everyone out, which is the
correct trade for never storing credentials or tokens at rest.

`navidrome.Identity` carries `user_id`, `username`, `is_admin`, a bearer
token, Subsonic token/salt, and the libraries that account may see —
read from Navidrome's `user_library` table, **failing closed** for
non-admins rather than over-sharing.

`workspace.py` turns an identity into a place on disk:

- `Workspace.key` is `<username-slug>-<library_id>`. The *id*, not the name,
  because a renamed library must not abandon its index.
- `staging/<key>/` holds `inbox/` (with `.incomplete/` inside it) and a
  four-line `.owner` marker naming the user, library name, library path and
  library id. `albums/` and `singles/` still hold Alex's 877 un-migrated
  files; nothing creates or writes to them any more.
- `config/beets/<key>/` holds that person's `config.yaml` and `library.db`.

The `.owner` marker exists because the inbox is drained on a timer with
nobody signed in — a file dropped in by hand has no session attached, so the
folder itself has to say who its files belong to. `inbox.drain_all()` reads
these back through `workspace.existing()`, and skips any workspace whose
library is not mounted in this container.
`prepare()` refuses if the marker names a different user, since two
usernames can slug to the same directory name.

**Why beets is split per person:** beets stores item paths *relative to* its
`directory`. One `library.db` genuinely cannot describe two roots.

---

## Identity, continued: which album a track is on

A track UUID says *which file*. An album UUID says *which record it belongs
to*, and getting that second answer wrong is where every identity bug in this
project came from.

It used to be inferred: one UUID per directory, plus a majority vote among a
file's neighbours. Both failed. `tools/ensure_uuid.py` run over a flat dump
of 746 loose tracks gave one UUID to 745 files spanning 101 albums, and
`stamp._choose_album_uuid` let nine arriving tracks outvote the one already
filed, splitting two records in half.

`registry.py` writes the answer down instead.

```
album_registry(library_id, album_key, album_uuid, created_at)
```

The first track of an album mints a `uuid4` and records it; every later track
- same job or months later, downloaded or dropped in by hand - looks it up and
gets the same answer. Membership stops depending on arrival order, directory
layout, or how many tracks happen to be present.

**The key normalises incidental variation only**: case, whitespace,
punctuation, unicode compatibility forms, and apostrophes (`Don't`, `Dont` and
the typographic spelling are one album - Spotify writes U+2019 and a
hand-typed tag writes the plain one). It makes no semantic judgement.
`Abbey Road` and `Abbey Road (Super Deluxe Edition)` stay **different**, which
is the deliberate reversal of the old `staging.album_key`: Spotify presents
them as two albums with two ids, and merging them puts two track 1s inside one
record.

**Per library by construction.** A UUID identifies a *file*, not a recording,
so Alex's copy and Kelly's copy of one album are different files with
different UUIDs, and an album key is only meaningful inside one library.

`repoint(library_id, old_key, new_key)` follows a retag. Ordinarily the album
keeps its UUID and only the key moves, so its Navidrome identity survives and
album-level stars and play counts survive with it. If the new key is already
mapped - that record is in the library, correctly tagged - **the incumbent
wins** and the retagged files adopt its UUID. It has to be that way round:
only the newcomer can be rewritten, so choosing the newcomer's value would not
move the established album, it would split it.

---

## Ingestion: one road in

Everything enters the library through one person's inbox, and one function
does the filing. That is the whole design. The album-versus-single routing,
the completeness check, the regrouping pass and `Non-Album/` were all
consequences of a download and a hand-drop taking different paths in, and each
was a place a track could end up somewhere nobody would look for it.

```
 a download                                   a hand-drop
     |                                             |
 spotify.py / generic.py                           |
     |  resolved job (list of tracks)              |
 worker.py                                         |
     |  ledger check                               |
     |  matcher.py   (skipped for a direct URL)    |
     |  downloader.py -> inbox/.incomplete/<job>/<item>.mp3
     |  tagger.py    (Spotify tags written on)     |
     |                                             |
     +--> inbox.deliver() --> inbox/ <-------------+   (SMB copy, drag-in)
                                |
                                |  inbox.drain(), polled every 15s, once a
                                |  file has been still for
                                |  staging_quiet_seconds
                                |
                         filer.file_track()
                                |
              +-----------------+------------------+
         read_meta()      registry lookup     write both UUIDs
         (its own tags)   (album_uuid)        (before the move)
                                |
                   $albumartist/$album/$disc-$track - $title.ext
                                |
                        navidrome.notify()
```

**Nothing gates it.** A finished track is playable within seconds, whether or
not anything can identify it. Beets used to be the admission test and it
refused 82% of what it was handed - 327 of Kelly's 400 tracks - almost all of
it for mechanical reasons that had nothing to do with the music: no DNS in the
container, a `max_rec.missing_tracks` cap, a `data_source` penalty against
MusicBrainz itself.

- **`spotify.py`** resolves a Spotify link to tracks with full metadata, and
  backs the Browse tab's search.
- **`generic.py`** handles anything else yt-dlp understands. Items carry a
  `direct_url`, so the worker skips matching entirely.
- **`matcher.py`** picks the YouTube Music recording corresponding to a
  Spotify track, scored on title, artist and duration, with hard gates on
  title and artist so one perfect signal cannot mask a fatal weakness in
  another.
- **`downloader.py`** fetches with yt-dlp and converts to MP3.
- **`tagger.py`** writes Spotify metadata. These used to be seed data for a
  matcher that would overwrite them; they are now **what the file is filed
  by** and what Navidrome shows. The `artist` tag is still the track's
  **primary** artist rather than the full credit - the full credit stays on
  the job, for the browser and the YouTube Music search.
- **`inbox.py`** is the door. `scratch_path()` is where a download is built,
  in a hidden `.incomplete/` *inside* the inbox, so arriving is a rename on
  one filesystem and the poller walks straight past a file yt-dlp is still
  writing. `deliver()` moves a finished download in and files it on the spot,
  because the worker knows that file is finished. `drain()` is the poller,
  for everything else.
- **`filer.py`** computes the path from the file's own tags, writes both UUIDs
  *before* the move so Navidrome never sees a track without one, and moves it
  into place. `os.replace` first, falling back to copy-and-delete on `EXDEV`,
  because scratch space and the library are separate bind mounts.

**The poller cannot race the worker.** A rename preserves mtime, so a
delivered file is seconds old and `settled()` is false for it for the whole
quiet period - long after `deliver()` has filed it. The same property means a
track the application died on is sitting in the inbox and gets filed on the
next start, instead of being discarded with the job's scratch directory.

### Paths are frozen

`$albumartist/$album/$disc-$track - $title.ext`, rooted at the library. The
disc prefix appears only on a multi-disc release; a file with no track number
is named for its title alone, not `00 - Title`. Collisions are numbered,
never overwritten.

After that, **no automatic process moves a file**. Navidrome identifies a
track by its UUID and groups albums by tag, never by path, so a path that
drifts from the tags is cosmetic - it matters only to a human browsing the
filesystem. Only a retag confirmed in the review page moves anything, and the
UUID means nothing is lost when it does.

Freezing paths is what removed most of the old machinery: `staging.regroup`,
`_prune_empty`, `_singleton_mode`, the album/single routing in `staging.plan`,
`stamp.resplit`, `spanning_albums` and the re-stamping after a move all
existed only because files moved.

---

## Library: everything you own, filtered

`library.py` and the Library panel. This was Review, which showed only
albums with something unconfirmed - and that made a matched album
**unreachable**: applying a release gave it MusicBrainz ids, dropped it off
the only list carrying the button, and left no way to correct a wrong
choice. So the list is everything and the narrowing is a filter.

The filter is named for what it is - **"no MusicBrainz match"**, not
"untagged". A doujin release or a bootleg can be tagged perfectly by hand
and will never have an ID, so those albums live in the filter for ever and
that is correct. The count is a statement about MusicBrainz, not a queue
that empties.

The album is the unit, and a row opens to show its tracks. The listing
**counts** tracks rather than building them - paging 2,800 albums to show
fifty rows would otherwise materialise 6,800 track objects - and one
album's tracks are read by their own query when a row is opened.

**Untagged means no MusicBrainz recording id**, read off Navidrome's database
every time. Nothing is stored, so a track leaves the list by gaining an id and
no flag can fall out of step with reality - which is exactly how the
`import_refusal` table went wrong.

Grouped by folder, because the filer puts exactly one album in one directory,
so the directory *is* the album. The action is album-level by default: a
per-track correction is what splits an album. **Per user, strictly private**,
scoped through `identity.libraries`; an admin does not see another person's
list.

Matching is manual, one album at a time. `POST /api/library/match` asks beets
for the candidate list that `quiet_fallback: skip` throws away, and is the
seam where Picard could replace beets later. `POST /api/library/match/apply`
retags the files **in place** - beets runs with `move: no` and `copy: no`, so
it tags and nothing else - and then `filer.after_retag()` re-points the album
UUID and the filer moves each file. One function decides where a track lives.

On day one the list is large and honest: 1,113 of Alex's 6,495 tracks have no
MusicBrainz id. That was always the number - beets was keeping it outside the
library rather than making it smaller.

**One consequence worth stating plainly.** This makes the library permissive:
mistakes now land inside it rather than being held outside. That is clearly
the right trade at an 82% false-rejection rate, but it means the review list
and the duplicates tooling stop being nice-to-haves and become the actual
quality mechanism.

---

## Companion features

- **`health.py`** — checks against Navidrome's database and the disk audit,
  scoped to the signed-in user's libraries via `_live_clause`. Nine rows
  that can be acted on, plus a handful marked `secondary` that the panel
  hides behind a toggle: things that can recur but rarely do, and facts that
  are status rather than health. Where the database and the disk answer the
  same question, the disk wins - Navidrome's index can be stale, the files
  cannot.
- **`diskaudit.py`** — walks the library reading tags directly, because
  Navidrome's index can be stale or wrong. Runs in the background and
  caches.
- **`duplicates.py`** — groups copies of the same recording by MusicBrainz
  id and by normalised title, **always within one library**. Ranks copies,
  migrates stars and ratings onto the keeper, and *quarantines* losers to
  `duplicates-removed/`. Nothing is deleted.
- **`playcounts.py`** — five-minute readings of Navidrome's cumulative play
  counts, so listening history stops being unrecoverable. Read back by the
  Listening panel, which resolves each stored UUID to a title through
  Navidrome's index; a track that has left the library still counts, because
  the plays happened. `coverage()` is per person, `status()` is per
  installation, and the difference matters - one account's imported history
  is not another's to read. Keyed by track
  UUID, storing only what changed, with a run log so a quiet day still
  counts as captured. `play_imported` holds the Last.fm backfill beside it.
- **`operations.py`** — long work that is not a download: a candidate
  lookup, a retag, a disk audit. One at a time per name, off the request,
  with a status the browser can ask for; starting one already running reports
  the one in flight rather than occupying a second of FastAPI's forty
  threads on a lock. Results arrive over the WebSocket.
- **`playlists.py`** — smart playlist rules. Translates between Navidrome's
  nested operator shape and a flat form the browser can render. Rules it
  cannot represent are marked unsupported rather than flattened.

---

## HTTP layer

`main.py` (~1,370 lines — it still wants splitting) holds every route. Three
background loops run for the life of the process: `_inbox_loop` (every 15s,
files what has been dropped in), `_audit_loop`, and `_snapshot_loop` (play
counts, every 5 minutes).

- A `require_session` middleware gates all `/api/*` except the three auth
  paths. `/healthz` is deliberately ungated so the container healthcheck
  works.
- `current_session` is the normal dependency; `admin_session` gates settings
  writes.
- Jobs are scoped per owner, and WebSocket events fan out only to the
  session that owns the job.
- The shell is served `no-store`; a cached pre-auth page once made the app
  look like it would not sign in.
- The review endpoints take a `library_id` and a `folder`, never a path.
  `review.album_dir()` is the boundary that turns those back into a
  directory: the library must be one this account can see, and the resolved
  path must sit under that library's root — resolved and compared rather than
  filtered for `..`, since a symlink walks out of a filtered name too. An
  empty folder is refused outright: `root / ""` is the root, so a file loose
  at the top of the library would otherwise have offered a matcher the whole
  library as one release.

---

## Front end

`app.js` is one global scope, organised by panel: queue, browse, review,
health, duplicates, playlists, listening, settings. It still wants splitting
(FIXES item 29).

The Library panel is the old Staging panel's markup and CSS classes with a
different question behind it — `staging-item`, `staging-name` and friends are
still the layout class names, and renaming them is cosmetic work nobody has
done. The ids were renamed (`#review`, `#review-op`, `#review-badge`)
because `test_frontend.py` fails the build on an id the stylesheet styles
that the markup does not have.

There is no JavaScript test runner — Node is not available here or in CI — so
`tests/test_frontend.py` checks the *joins* instead: no duplicate ids, every
id `app.js` names exists in the markup, every id the stylesheet styles
exists, every menu entry has a section behind it, and nothing is left in the
markup that nothing references. It cannot tell you the page works. It can
tell you the page is still wired to itself, which is what the hand check
before each deploy was actually doing.

A message about work that a panel started belongs *in that panel*.
`#error` at the top of `<main>` sits outside every section, so anything left
there followed you onto every other view; `showView` now clears it, and the
retag and audit outcomes go to `#review-op` and `#health-op`.

Navigation is a single menu at every width: a button in the header opens a
full-height overlay listing the panels. Panels are shown by toggling
`hidden` on `#view-<name>` sections, driven off the nav buttons themselves
rather than a hand-kept list — an earlier version named a view that no
longer existed and the resulting throw hid every panel at once.

The header carries the menu button, the current view's name and the
connection pill; the username and Sign out live in the overlay. The view
name matters: with no tabs on screen it is the only thing saying where you
are.

The menu hangs from the header's bottom edge rather than covering it, at a
z-index *below* the opaque sticky header. Its first version was `inset: 0`
with a title bar of its own, which meant opening it swapped one header for a
different one in the same place. The panel's top offset comes from
`--header-h`, measured in JS on load, resize and open, because the height
moves with the safe-area inset and the 720px breakpoint.

Settings is a view like any other (`#view-settings`), not a form that
toggles on top of whichever panel is showing. Every nav item's `data-view`
has a matching `#view-<name>` section and nothing else does - the header's
own title is `#current-view` precisely so it cannot be mistaken for one.

`style.css` is token-based: colours, spacing scale, radii and shadow all
come from `:root`, with one button in three weights (filled, outlined,
bare). The `@media (max-width: 720px)` block is now only about *content* —
forms stacking, grids collapsing, long strings wrapping. Navigation is the
same at every width, which is what the burger bought: roughly ninety lines
of phone-only nav went with it, including a 6rem overhang to cover the iOS
home indicator, a `font-size: 0` trick to swap in short labels, and absolute
badge positioning that had already been wrong twice.

---

## Deployment

Push to `main` → GitHub Actions builds an arm64 image under QEMU and pushes
to `ghcr.io/awdimartino/download_center:latest` → pull on the Pi.

```bash
ssh -i ~/.ssh/id_ed25519_pi argyle@alex-pi \
  'cd ~/Docker && docker compose pull download-center && docker compose up -d download-center'
```

There is no git checkout on the Pi; `~/Docker/download-center/` holds only
`config/`. The README describes this same pull-and-restart flow, without the
host specifics.

---

## Gotchas, learned the hard way

Each of these cost real time or caused a real bug.

**Navidrome**
- `computePID` hashes `""` for a missing tag. A single-field PID chain
  collapses every untagged file into one track.
- Vanished directories are marked missing on the **folder** row, not on
  `media_file` rows. Health queries must join `folder` or they over-count.
- Stamping preserves mtime, so an incremental scan never re-reads the file.
  A **full** scan is required after tag changes.
- Duplicate grouping must be per library. An early version grouped across
  libraries — 74 of 271 groups spanned both, and a bulk resolve would have
  deleted Kelly's music.
- Migrating a star as admin creates an invisible admin star and quarantines
  the real one. Act as the owning user.

**beets**
- Item paths are stored **relative to `directory`**. Resolving them against
  the wrong root silently does nothing.
- `--pretend` output is colourised even when redirected. Set
  `ui: color: no` and strip ANSI anyway.
- A YAML plain scalar cannot start with `%`, so `%if{}` path templates must
  be quoted.
- `$album_artist_no_feat` is not a real field. An invalid field name files
  albums into a literally-named folder.

**Audio files**
- MP4 freeform atoms must use the `com.apple.iTunes` mean or TagLib will not
  expose them.
- `.wav` and `.aiff` cannot carry these tags; exclude them from tag checks.

**Browser caching**
- A response with **no `Cache-Control` is still cacheable**. The browser
  invents a freshness lifetime — conventionally a fraction of the file's age
  — and does not ask again until it expires. Starlette's `StaticFiles` sends
  an ETag and a Last-Modified and no Cache-Control, so `index.html` being
  `no-store` bought nothing: a deploy served a new shell beside an `app.js`
  Safari saw no reason to re-fetch, and a button that was in the file the
  container served was absent from the page.
- Headers alone cannot fix a browser that is *already* holding a stale copy,
  because it does not ask. Only a URL it has never seen can. Both are in
  place now: `no-cache` on `/static` (which means "revalidate", not "do not
  store" — an unchanged file still answers 304) and `?v=<hash of the
  assets>` stamped onto the references by the shell, which is the one
  document that is always fetched fresh.

**CSS**
- Every size, weight, gap and colour comes from a token in `:root`. Four
  tests fail the build on a raw px font-size, a bare weight, a raw gap or a
  hex outside `:root` - the panels were built over months and each decided
  for itself what "small" meant, ending at thirteen font sizes.
- **Form fields must be at least 16px.** Below that, iOS Safari zooms the
  page when one takes focus, and does not zoom back.
- `[hidden]` is a user-agent rule and loses to any class setting `display`.
  Stated once, `!important`, at the top of the stylesheet.
- `overflow-x: hidden` on `html`/`body` makes them scroll containers, which
  breaks `position: sticky` for descendants — and on WebKit knocks
  `position: fixed` loose. `clip` breaks sticky in Chromium too. Do not set
  it; handle long strings with `overflow-wrap`.
- `backdrop-filter` makes an element a containing block for `position: fixed`
  descendants. Blurring the header pinned the bottom tab bar inside it. The
  bar is gone, but the menu overlay is fixed too, so the header is still
  deliberately unfiltered.
- On iOS a fixed bottom bar sits at the *layout* viewport bottom, which is
  not the screen bottom. Extend the element's own box past it; a
  same-coloured `box-shadow` is not painted there. **No longer in play** —
  the burger replaced the bottom bar and took this workaround with it. Kept
  because anything pinned to the bottom on iOS will meet it again.
- `env(safe-area-inset-*)` is still needed without a bottom bar: the header
  pads for the notch and the menu panel is full-height, so it pads for both.

**Container boundaries — a path that resolves is not a path that persists**

This is the shape of two separate bugs, so it is worth stating as one idea.
Navidrome reports where a library lives from *its* database, and this
application is a different container with its own mounts. A path can
therefore be perfectly valid for Navidrome and absent here — and a Linux
container will happily create a missing directory in its own writable layer
and let you write to it. Everything succeeds. The next `docker compose pull`
destroys it.

- **Adding a library to Navidrome means adding a bind mount here too.** Both
  containers, at the same path, so a library path read from Navidrome's
  database is directly usable with no mapping to maintain. Without it,
  `directory:` in that person's beets config names a path that exists only
  inside the container: beets creates it, files the music into it, reports
  success, and the tracks go into the ledger so nothing ever asks for them
  again. `workspace.require_mounted` now refuses this, and `for_session`
  calls it by default — read-only callers opt out with
  `require_library=False`.
- **The duplicates quarantine hit the same trap.** It resolved to
  `music_dir.parent / "duplicates-removed"`, which is `/duplicates-removed`,
  which is not a volume. Worse, being on a different filesystem from the
  library made `shutil.move` a copy-then-unlink, so the original really was
  removed. It lives at `<library root>/duplicates-removed/` now, on the same
  filesystem, with a `.ndignore` so the scanner skips it.

The general rule: before writing anything outside `/config` or `/downloads`,
check that the destination is on a mount, not merely that the path resolves.

**Async jobs**
- A job's task must be cancelled *and awaited* before its files are removed.
  `cancel()` only schedules the CancelledError; the task still has to reach
  a suspension point and run its own cleanup. Deleting first is a race the
  filesystem loses - yt-dlp recreates its output directory, renames fail
  with ENOENT, and the retry loop runs for ever.
- The download gate in `worker.gate()` is process-wide, which is what makes
  `concurrency` mean what it says. The cost is that one task which never
  releases starves every later job of every user. Anything holding a permit
  must be guaranteed to release it, so orphaned tasks are not a tidiness
  problem here - they are an outage.

**This environment**
- `python` is not on PATH. Use `.venv/Scripts/python.exe`.
- Some bash heredocs fail in this harness; write files with the editor tool
  instead.
- Files are CRLF; git warns on every commit. Preserve line endings when
  rewriting a file programmatically.
