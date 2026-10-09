# Navidrome Companion — every feature, and how it works

What each part of the app does, what you see, and what happens underneath:
which store answers each question, what gets written where, and why it was
built that way. For installing it, see [SETUP.md](SETUP.md). For what is
planned, see [PLAN.md](PLAN.md); for known defects, see
[CODE_REVIEW.md](CODE_REVIEW.md).

Written 2026-10-04 against the code as it stands.

---

## Contents

1. [Principles](#1-principles)
2. [How it fits together](#2-how-it-fits-together)
3. [Accounts, sign-in and navigation](#3-accounts-sign-in-and-navigation)
4. [Home and listening history](#4-home-and-listening-history)
5. [Download: search, recommendations, the queue](#5-download-search-recommendations-the-queue)
6. [Drop and the inbox](#6-drop-and-the-inbox)
7. [Filing and identity](#7-filing-and-identity)
8. [Library](#8-library)
9. [Duplicates](#9-duplicates)
10. [Health](#10-health)
11. [Playlists](#11-playlists)
12. [Settings](#12-settings)
13. [Background work and long operations](#13-background-work-and-long-operations)
14. [API reference](#14-api-reference)
15. [Gotchas learned the hard way](#15-gotchas-learned-the-hard-way)

---

## 1. Principles

The app does what Navidrome can't, in a browser, from a phone or a computer:
the gaps (downloading, smart-playlist rules, listening history Navidrome
throws away) and the library maintenance otherwise done over SSH. Five rules
run through everything below.

1. **Navidrome's database is read-only.** Every write to Navidrome goes
   through its API, as the signed-in user. There is no second copy of who
   exists, who can see what, or what is starred.
2. **Everything is per user**, derived from Navidrome's own account records:
   which library a download lands in, whose stars a duplicate carries, whose
   playlists these are.
3. **Nothing is deleted.** Files leave the library by being moved to a
   quarantine folder, with a record of where they came from.
4. **Identity is the UUID.** A `navidrome_uuid` tag on each file survives
   re-tagging, moving and a rebuilt database. Anything that refers to a track
   over time refers to it by UUID.
5. **The file is the truth.** Navidrome's index is a cache of the files; this
   app's own database holds only what cannot be observed from them.

Everything works on a phone. The maintenance jobs are exactly the ones you
want to start from the sofa.

---

## 2. How it fits together

One FastAPI process serves a JSON API, a WebSocket event stream and a
static front end. It runs as one container beside Navidrome, sharing the
library volumes and reading Navidrome's database file.

```
browser ──HTTP/WS──▶ companion ──read-only SQLite──▶ navidrome.db
                         │
                         ├──HTTP (native + Subsonic API)──▶ Navidrome
                         ├──in-process──▶ yt-dlp, mutagen, Spotify, YouTube Music
                         ├──subprocess──▶ ffmpeg, beets, rsgain
                         └──filesystem──▶ inbox ─▶ library
```

The front end has no build step and no framework: `app/static/index.html`,
`style.css`, and twenty ES modules under `app/static/js/` with `main.js` as
the only entry. The Library panel is five of them, `library*.js`;
`library-shared.js` imports none of the others, which is what keeps their
imports of one another safe. The shell is served `no-store` and stamps
`?v=<hash>` onto its asset URLs, so a deploy reaches the browser on a normal
reload.

### Where state lives

Knowing which store owns a fact is most of understanding the code.

| Store | Owns | Access |
|---|---|---|
| `navidrome.db` | accounts, libraries, who sees which library, stars, ratings, running play counts, the scanned index | **read-only**; writes go through Navidrome's API |
| `config/state.db` | album identity registry, listening history, review marks, duplicate decisions, the quarantine record | ours alone (`store.py`) |
| `config/.cover-survey.json` | which album covers the last survey found barred, by folder and file stamp | a cache; deleting it costs one slow survey |
| `config/beets/<user>-<library>/` | one person's beets config | used only by *Find matches*; each apply uses a throwaway index |
| the audio files | every tag, including both UUIDs | the truth |
| process memory | sessions, the download queue, long operations | lost on restart, deliberately |

`state.db` tables:

| Table | Holds |
|---|---|
| `album_registry` | `(library_id, album_key) → album_uuid`. Which record a track belongs to |
| `play_snapshot` | play counts as read from Navidrome, only when they changed |
| `play_snapshot_run` | one row per day the collector ran, so a quiet day still counts as read |
| `play_anomaly` | play counts that went down |
| `play_collection` | when play counts were first read: the baseline |
| `play_imported` | listening from before collection began (Last.fm) |
| `album_reviewed` | albums someone has dealt with in Library |
| `duplicate_dismissed` | "keep both" decisions, and whose |
| `duplicate_quarantined` | every file set aside: from where, to where, who decided, the copy kept |
| `ledger` | legacy download record; no longer read, left on disk rather than dropped |

---

## 3. Accounts, sign-in and navigation

### Signing in

You sign in with your **Navidrome account**. The form posts to Navidrome's
own `/auth/login`; the app never stores the password. What comes back is
held in memory as a session: your user id, name, whether you are an admin,
a bearer token for Navidrome's native API, a Subsonic token and salt, and
the libraries your account may see.

- **Libraries** are read from Navidrome's `user_library` table. If that
  table is missing (Navidrome older than 0.58), admins see every library
  and everyone else sees none — it fails closed rather than over-sharing. A
  session with no library re-checks at most once a minute, so access granted
  in Navidrome shows up without signing out.
- **Lifetime** is 14 days, measured from when you were last seen, and never
  more than 30 days from sign-in; expired sessions are swept every five
  minutes. The cookie is sent again at most once a day while you use the
  app, so the browser keeps it as long as the server keeps the session.
- **Admin status and libraries** are re-read from Navidrome every five
  minutes, so a demotion or a revoked library reaches an open session, and
  an account deleted in Navidrome is signed out. The cookie (`nc_session`; `dc_session` before 2026-10-09) is
  `httponly`, `samesite=lax`, and `secure` when the request came over HTTPS.
- **A restart signs everyone out.** That is the trade for never storing
  credentials or tokens at rest.
- Every `/api/*` route needs a session except the three sign-in routes;
  `/healthz` is open for the container healthcheck.

**Administrators** are whoever Navidrome says is an admin. Admin-only:
changing Settings, and forcing a play-count reading.

### Navigation

One menu button at every width opens a full-height panel: Home, Library,
Download, Drop, Playlists, Duplicates, Quarantine (with a quiet count of
what is set aside, not a badge), Health, then Settings, your name and
the libraries you can see, and Sign out. The header shows the current
view's name and a connection pill (live/offline, from the WebSocket).

Badges on the menu entries:

- **Library** — albums that need review.
- **Duplicates** — duplicate groups, once the panel has been opened.
- **Health** — rows that need action. Health includes a duplicates row, so
  duplicates reach the menu dot before that panel is ever opened.

A dot on the menu button shows when any badge is showing. Health refreshes
every five minutes in the background.

---

## 4. Home and listening history

Home is a dashboard of your own listening, with the full statistics
(formerly the Listening tab) at the bottom of the same page.

### What you see

**Greeting** — "Good morning/afternoon/evening, *you*." from the browser's
clock ("Still up" before 5am).

**Headline fact** — one of several facts, picked at random on each visit:
different tracks played this year, hours of music this year, plays this
month, most-played artist of the last year, artists heard in twelve months,
busiest month ever, library size. Facts that would read zero are left out.

**Cover** — a large album cover chosen by weighted lottery from up to 40
albums: up to 20 played in the last fortnight (weight 4), up to 10 more from
the last month (weight 2), the rest from the twelve-month top (weight 1). So
the cover is usually something you played lately. If an image fails to
load, the next album is tried.

**Theme from the cover.** The cover is sampled on a 48×48 canvas, colours
are bucketed and scored by frequency, saturation and lightness, and the
winner becomes the accent colour for the **whole app**, not just Home. A
greyish cover gives a warm neutral instead. The palette is remembered in the
browser so other pages start themed.

**Tiles:**

- **This month** — plays this month, and the change on last month. Tap to
  scroll to the monthly chart.
- **In *year*** — different tracks played this calendar year and roughly how
  many hours. Tap to jump to the statistics, showing that calendar year
  (as Dates, 1 January to 31 December) - not the Year button, which is the
  last 365 days.
- **Your library** — tracks and albums in the libraries you can see. Tap to
  open Library.

**Plays by month** — 24 months as an area chart, peak marked, hover for the
figure. **Most played artists** — the top six over the last twelve months,
by track artist.

If play counts have not been read for 30 minutes, a banner says so: listening
since then is not being recorded.

### The statistics section

A range switch — **Today**, **Month** (30 days), **Year** (365 days), **All
time**, or **Dates** with a from/to picker — drives everything in this
section from one request:

- **Coverage tiles** — plays in view, imported history (if you have any),
  days the collector has run, and whether it is collecting now.
- **Longest session** and the number of sessions in view. A session is a run
  of plays with no gap of 30 minutes or more. Sessions are found over your
  whole history first and then filtered to those that *started* in range, so
  a range boundary never splits one evening into two short ones.
- **By hour of day** — 24 bars in your configured timezone.
- **Top albums** and **top genres** (ten each; genre is the track's first
  genre tag).
- **Most played tracks** — ten, expandable to fifty. A track that has left
  the library still counts, shown as "no longer in the library", because the
  plays happened.

The monthly chart and top artists above stay on their own fixed windows; the
range switch was never meant to shorten them.

### How listening history is recorded

Navidrome keeps only a **running total** per track and the time of the
**most recent** play. History it has overwritten is gone. So the app reads
those totals itself and keeps the history.

**Every five minutes** (`_snapshot_loop` → `playcounts.take`), the app reads
Navidrome's `annotation` table for every user, joined to the live tracks
(the file not missing, *and its folder not missing* — Navidrome marks
vanished directories on the folder row). Each row is keyed by the track's
`navidrome_uuid`, not Navidrome's row id, so history survives a rebuilt
database. Tracks without a UUID are counted and skipped.

**Only changed counts are stored** in `play_snapshot`. A full read is a few
thousand rows; most readings change a handful. At five-minute intervals the
total almost always rises by exactly one, and the row carries Navidrome's
`play_date` — the exact moment of that play. That is what turns a running
total into a log of individual plays, and what makes sessions and the hour
chart possible.

- A count that **falls** (a re-import, a reset) is recorded in
  `play_anomaly` rather than counted as negative plays.
- Each day the collector ran gets a row in `play_snapshot_run`, so a day with
  no listening still reads as collected.
- If Navidrome's database cannot be opened, nothing is written, and after 30
  minutes Home shows the warning.
- An administrator can force a reading with `POST /api/playcounts/snapshot`.

Rows from before September 2026 were read nightly and carry a bare date;
newer rows carry a full timestamp. Both live in one column: a date sorts
before every timestamp on the same day, so **day ranges are half-open** —
`taken_on < next_day(D)`, never `<= D`, which would drop the whole of day D.

**Imported history.** `play_imported` holds listening from before collection
began: one account's Last.fm history (49,524 scrobbles back to 2022, of
which 41,203 plays matched the library), imported once with
`python -m app.lastfm`. It is kept apart from snapshots because it is a
different kind of evidence — somebody else's record matched by artist and
title, not a count this app read. `--times` later recovered each scrobble's
exact time for rows already imported, without re-matching: it only replaces
a day's row with timestamped rows when the scrobbles resolve to exactly that
track, and rolls back if the total would move. Scrobbles from the moment
collection began onwards are left to the snapshots, and a plain import is
refused once `--times` has run, since it would write day rows beside the
timestamped ones.

**Days and hours** are cut in `play_day_timezone` (default UTC).

### How the numbers are computed

Everything comes from one list of plays (`playcounts.increments`), used by
Home's top half and the statistics section alike. It walks each track's
snapshot rows in order and counts each rise, dated by that reading's
`play_date` in `play_day_timezone` (the reading's own time stands in when
there is none), and appends the imported history. A range is the plays
whose local day falls in it, so both halves of the page agree to the play.

A track's first row is a baseline only if it came from the reading when
collection began, recorded in `play_collection` by the first reading ever
taken (even one that found nothing to store). Those rows are lifetimes,
already covered by the imported history; a track's first row from any later
reading counts from zero.

A count that falls and rises again counts the rise: 10, 4, 6 is two plays.

**Caching** (`memo.py`). Home's figures are kept in memory with the version
of the data they were computed from: the row counts and highest row ids of
the history tables, the track index's version, and today's date. Nothing is
invalidated by hand — a new play, a hand `DELETE` or midnight each change the
version. The track index (names, albums, durations, genres for every UUID)
is one scan of Navidrome's `media_file`, redone only when Navidrome's
database or WAL file changes on disk, and its version moves only when the
result differs; Navidrome writes on every play, so tying it to the file
would throw everything away after each song. After a reading records new
plays, the loop precomputes Home for whoever played them. A warm Home costs
about 15 ms on the Pi, against about 2 s uncached.

---

## 5. Download: search, recommendations, the queue

### Search, or paste a link

The Download tab (called Browse until 2026-10-08; it is still `browse`
in the code) has one box: **"Search Spotify, or paste a link"**. Text that starts
with `http://` or `https://`, or is a Spotify link without it
(`open.spotify.com/…`, `spotify.link/…`, `spotify:album:…`), is a link —
the button reads **Download** and a hint says what kind of link it looks
like. Anything else is searched on
Spotify as you type (after a short pause, from two characters), filtered by
the chips **All / Albums / Songs / Artists**.

- **All** shows a few artists, a few songs and every album returned, each
  with "See all".
- An **album** opens in a side panel: cover, kind (Album, EP, Single,
  Compilation), year, length, and its tracks, each with its own Download
  button.
- An **artist** opens their discography, split into Albums and Singles and
  EPs, with reissues of one title collapsed to the earliest.

An account with more than one library gets an **Into** choice under the box
(and on the Drop page): the library new downloads and uploads go into. The
two pages share it and the browser remembers it. With one library there is
no choice to make, and nothing is shown. The "in library" markers below
still read the first library.

### Recommendations

While nothing is searched, the tab shows shelves of things to download
next (`app/recommend.py`, drawn by `foryou.js`). Each card is an ordinary
result: it opens, downloads and shows its state the same way.

- **New from your artists**: releases from the last 90 days by your
  favourite artists.
- **Missing from your artists**: their albums (not singles) that your
  library has no edition of. "OK Computer (Collector's Edition)" counts as
  the OK Computer you have. Up to three per artist, taken in turns.
- **Artists like yours**: Last.fm's similar artists for your top eight,
  minus anyone already in the library, ranked by how many of your
  favourites point at them ("Like Radiohead and Björk").
- **Because you played "…"**: Last.fm's similar tracks for up to three
  songs on repeat in the last 30 days, by other artists, skipping songs you
  hold.
- **More *genre***: Last.fm's top albums for your three most played
  genres, one per artist, not in the library.

*Favourites* are the last year of plays per track artist, plus stars and
ratings of 4 or 5 (a starred artist counts as 40 plays, a starred album 20,
a starred track 5). The last three shelves need `lastfm_api_key`. Spotify
withdrew its recommendation and related-artist endpoints from new apps, so
the judgement of "similar" is Last.fm's, and Spotify only supplies the
cards.

A pass costs about a hundred requests, so it runs **in the background, at
most once a day per person**, and is stored in `state.db`
(`recommendation_cache`). A visit after a day serves the old pass and starts
a new one. **Refresh** starts one now. What the library holds and what you
dismissed are applied when the shelves are read, so a just-downloaded album
leaves its shelf on the next load.

**✕ (not interested)** hides a card for you only (`recommendation_dismissed`).
The toast offers **Undo** and **Hide all by *artist***, which hides that
artist from every shelf.

### "In library" markers

Results are marked with what your library already holds. This is computed
from **Navidrome's index**, not from a record of downloads: the live tracks
in your first library, keyed by normalised artist and title (both the track
artist and the album artist). A song is held if either its full artist
credit or its primary artist matches. Albums show "N of M in library".

It is a note, not a gate. **Nothing refuses a download** — re-queuing an
album you have makes second copies, and the album panel warns you first ("N
of M are already in your library and would arrive as second copies"). The
match is deliberately approximate (a live version counts as held), and it
reflects Navidrome's last scan, so a track filed seconds ago shows as held
through the job's state until Navidrome rescans.

There used to be a download ledger that skipped anything once fetched. It
was removed: a track that later left the library became permanently
unfetchable, reported as "skipped" with nothing to say why, and a CD rip was
never in it anyway. Asking Navidrome is both the right question and a
broader answer.

### What links work

**Spotify** track, album and playlist links, as URLs (with or without the
scheme, an `/intl-xx/` or an `/embed/` segment), `spotify:` URIs, or the
app's `spotify.link` short links, which are followed when the job resolves.
A job keeps the plain `https://open.spotify.com/…` form of the link. **Artist links are refused** —
a discography is rarely what you meant. Playlists skip podcast episodes and
local files. Jobs over 500 tracks are refused; queue them in parts.

**Anything yt-dlp understands** — YouTube, YouTube Music, SoundCloud,
Bandcamp and hundreds more — including playlists, from the public internet
only: a link to an address on a private network is refused. These skip matching,
because the URL already names the audio. Tags come from what the site
publishes: YouTube Music, Bandcamp and `- Topic` channels expose real
track, artist and album fields; a plain upload offers only a video title,
so artist and title are guessed by splitting on " - " after stripping noise
like "(Official Video)" — which follows convention, and gets it backwards
on a title like "Game Over - Free Metal Instrumental". With no album, the
title doubles as the album.

### What happens to a Spotify download

1. **Resolve.** The link becomes a list of tracks with full metadata:
   title, full artist credit, primary artist, album artist, album, track and
   disc numbers, release date, duration, ISRC and cover. Album and playlist
   tracks are fetched again in batches of 50 to get their ISRCs.
2. **Match** (`matcher.py`). YouTube Music is searched for "artist title",
   songs first and then videos (scored at 95%). Each candidate is scored:

   | Part | Weight |
   |---|---|
   | title similarity | 0.40 |
   | artist similarity | 0.25 |
   | duration: 1 − (seconds off ÷ 15) | 0.30 |
   | it is a song, not a video | +0.04 |
   | the album name matches | +0.06 |
   | each version marker the Spotify title lacks ("live", "remix", "sped up", "karaoke", "nightcore", …) | −0.25 |

   Title and artist are **hard gates**: below 0.60 and 0.50 a candidate
   scores zero, so one perfect signal cannot hide a fatal one. "feat."
   clauses are removed before comparing. The best candidate must score
   **0.70**, or the track fails with the score and title it did find.

   A wrong file is far more expensive to find and undo than a missing one
   is to fetch, so a doubtful match fails rather than downloads, and you can
   retry it.

   **Other sources** (`sources.py`, since 2026-10-09). SoundCloud is
   searched too, for every track, and Bandcamp is the last resort. Both are
   scored the same way, with one extra rule: the length must be known and
   within 15 seconds, because a major label's SoundCloud upload is a
   30-second preview with the right title and artist. A re-upload naming
   the artist in its title ("Radiohead - Karma Police", "Teardrop ●
   Massive Attack") is read both ways round. Bandcamp's search does not
   give lengths, so only its three best-named results are opened to check.

   **Which copy.** When YouTube Music and SoundCloud both have a
   *confirmed* match (score at least 0.90, length within 3 seconds), the
   one that sounds better is downloaded: each offered format is rated in
   MP3-equivalent kbps (Opus × 1.5, AAC × 1.3, lossless as 1411), so an
   artist's downloadable original on SoundCloud beats YouTube's Opus, and
   YouTube's Opus beats SoundCloud's 128 kbps MP3. Otherwise the order is
   YouTube Music, SoundCloud, Bandcamp. Whatever fails, a match or a
   download (a 403 from YouTube), the next source is tried, and a track
   that fails everywhere says what each source answered. The Queue names
   the source while it is tried ("Trying SoundCloud") and on a finished
   track that did not come from YouTube Music ("Done · Bandcamp").
3. **Download.** yt-dlp fetches the best audio and ffmpeg converts it to MP3
   at the configured bitrate (320 kbps by default) - or at 192 when the
   source is no better than that, which is most of YouTube (Opus at
   130-160 kbps). A source whose bitrate is not reported keeps the
   configured one. If
   `/config/cookies.txt` exists it is used, checked on every download.
4. **Tag** (`tagger.py`). Every existing tag is cleared and ID3v2.4 written:
   title, artist, album artist, album, track `n/total`, disc, date, ISRC,
   the Spotify id, and the cover embedded as front art. Covers are squared
   first (see [Covers](#covers)). The **artist tag carries only the primary
   artist**, from Spotify's structured artist list; the full credit stays on
   the job. If one track of an album carried "Michael Jackson, Paul
   McCartney" and the rest "Michael Jackson", the tracks would disagree and
   matchers read the album as a Various Artists compilation.
5. **File.** The finished file is moved into the person's inbox and filed on
   the spot (section 7).
6. When the job ends, Navidrome is asked to scan.

### Retries, limits and pacing

- **Retries**: up to `max_attempts` tries per track, waiting 2, 8 and 30
  seconds. A search that *could not run* (a rate limit, a network error) is
  retried; a search that ran and found nothing good enough is not, since the
  same search returns the same results.
- **Concurrency** is one gate for the **whole server**, shared by every job
  and every user: `concurrency` downloads at once (default 3). After each
  track the slot pauses `rate_limit_sleep` seconds (default 2).
- **Up to five active jobs per person**; finished jobs beyond the latest 40
  are forgotten.

### The Queue rail

Beside the results (a pill and bottom sheet on a phone) are your jobs, in
three groups: **active**, **needs a look** (failed, partial or cancelled,
with *Retry failed*), and **finished** (folded; *Clear* forgets them, and
nothing is deleted from the library). A card shows the cover, progress, and
"x of N · current track"; open it for every track's step — Waiting, Finding
a match, Downloading, Retrying, Tagging, Done, Failed — with the error on
hover. A track filed without its identity tags (the UUID write failed)
shows **Done, no identity**, and its finished card says how many: it is
playable, but stars and play counts cannot follow it.

The ✕ on an active job **cancels** it: it moves to *needs a look* as
Cancelled, with what it finished and *Retry failed* for the rest. Tracks
already filed stay in the library. The ✕ on a finished job clears it from
the list; for a job still running, its task is cancelled **and awaited**
before its scratch files are deleted (deleting first once left yt-dlp
recreating its directory forever while holding a download slot, which
stalled every later job of every user).

Progress arrives over the WebSocket, to the job's owner only: the whole job
at each phase change, and twice a second only the items whose status,
progress or error changed.

**The queue is in memory.** A restart forgets it. Files that had already
reached the inbox are filed on the next start; re-paste the link for the
rest.

---

## 6. Drop and the inbox

### The inbox

Everything enters the library through one person's **inbox**, and one
function does the filing. Each person has a workspace per library:

```
<scratch>/
  alex-1/                   <username>-<library id>
    .owner                  who this belongs to, and where it files to
    inbox/                  drop music here; it does not stay
      .incomplete/          downloads are built here, hidden from the poller
      upload-<id>/          one folder per browser drop
    leftovers/              covers, cue sheets and logs the filing left
```

The workspace key uses the library's **id**, not its name, so renaming a
library does not abandon anything. `.owner` exists because the inbox is
drained with nobody signed in: a file copied in by hand has no session, so
the folder itself must say whose it is. Two usernames can reduce to the same
folder name, so a workspace refuses a user its marker does not name.

Every 15 seconds a poller walks every workspace whose library is mounted
and files each audio file that has sat **unchanged for
`inbox_quiet_seconds`** (default 120). The quiet period is what makes a slow
SMB copy safe, and it is also a safety net: a download the app died on, if
it had reached the inbox, is filed on the next start. A file that fails to
file is not retried until its size changes. Loose audio sitting at the top
of the library root is picked up the same way. When a pass files anything,
Navidrome is asked to scan, as it is when a download or a Drop finishes.

On start-up, anything left in `.incomplete/` is deleted: the queue is in
memory, so nothing would ever finish or discard a download a crash
interrupted.

Once a folder in the inbox has settled with no audio left in it, its known
residue (images, cue sheets, rip logs, playlists, checksums) is moved to
`leftovers/` beside the inbox, keeping its path, and the folder is pruned.
Nothing is deleted, and a file the inbox does not recognise stays where it
is. `leftovers/` is yours to clear.

### The Drop page

Drag files or a whole folder onto the page, or choose files. Each drop is a
batch: files upload one at a time with progress, then the batch reports
"x filed · y failed · z still settling".

Underneath, each file is posted to `POST /api/inbox/upload` and written
into a per-drop folder `inbox/upload-<id>/`, keeping its relative path.
Uploads are capped at 200 MB a file, streamed to disk in chunks, and only
audio and cover images are accepted. When the last file of the drop has
arrived, `POST /api/inbox/upload/finish?batch=<id>` **backdates** the whole
drop past the quiet period and drains the inbox at once: unlike an SMB copy,
the browser knows when every byte has arrived. Backdating each file as it
landed let the poller file an album's tracks before its cover had uploaded.
If the tab is closed first, the poller files the drop once it has been still
for the quiet period.

The per-drop folder exists for covers: the filer copies a `cover.jpg` it
finds beside a track into that track's album folder, and if two drops shared
one folder, one album's art could be carried onto another's tracks.

---

## 7. Filing and identity

### Where a file goes

`filer.file_track` decides every path, from the file's **own tags**:

```
<library>/<album artist>/<album>/<disc>-<track> - <title>.<ext>
```

- The disc prefix appears only on a multi-disc release; a track with no
  number is named by title alone.
- No album artist falls back to the artist; no album goes to `Unknown
  Album/`; a file with no readable tags goes to `Unknown Artist/Unknown
  Album/`, and is visible in Library like anything else. Nothing is held
  outside the library waiting to be identified.
- Names are made safe for any filesystem: reserved characters become `_`,
  components are capped at 110 characters, Windows device names are
  escaped.
- **Collisions are numbered, never overwritten** — `Title (2).mp3` up to
  `(99)`, then it refuses.
- Moving uses a rename, falling back to copy-and-delete when scratch space
  and the library are different filesystems.
- A folder cover beside the source is copied along, unless the source sits
  at the top of the inbox or the library, where a cover belongs to no album
  in particular.

**Paths are frozen.** After filing, no automatic process moves a file.
Navidrome identifies a track by its UUID and groups albums by tag, never by
path, so a path that drifts from the tags is cosmetic. Only an edit you make
in Library moves anything — and the UUID means nothing is lost when it does.
Freezing paths is what let the old regrouping, re-splitting and re-stamping
machinery be deleted: all of it existed only because files moved.

**Nothing gates the library.** A finished track is playable within seconds,
whether or not anything can identify it. beets used to be the admission
test and refused 82% of what it was handed — almost all for mechanical
reasons that had nothing to do with the music. The cost of letting
everything in is that mistakes land inside the library, which is why the
Library's review tools and the Duplicates panel are the real quality
mechanism.

### Track identity: `navidrome_uuid`

Every filed file carries two tags, written **before** the move so Navidrome
never sees a track without them:

- `navidrome_uuid` — this file. Minted once, never changed.
- `navidrome_album_uuid` — the record it belongs to.

Navidrome is configured to derive its persistent ids from them (SETUP.md,
section 3), which is what makes stars, ratings and play history survive
re-tagging, moving and a rebuilt database.

- **MP3**: `TXXX` frames, keeping the file's ID3 version.
- **MP4/M4A**: freeform atoms under the `com.apple.iTunes` namespace. TagLib,
  and therefore Navidrome, does not read any other namespace; an early
  version wrote `com.navidrome`, which Navidrome never saw. That legacy form
  is still read and replaced on write.
- **FLAC, Ogg, Opus and others**: Vorbis/APE keys.
- **WAV and AIFF** cannot carry these tags and are skipped.

Every write is read back to check it. A file whose write failed is still
filed, and reported.

### Album identity: the registry

A track UUID says which file. An album UUID says which record it belongs to,
and getting that second answer wrong is where every identity bug in this
project came from. It used to be inferred from directory layout and a vote
among neighbours; both failed (one run gave a single UUID to 745 files
spanning 101 albums).

So it is written down. `album_registry` maps a normalised `(album artist,
album)` key, per library, to an album UUID. The first track of a record
mints one; every later track — same job or months later, downloaded or
dropped in — looks it up and gets the same answer. A file arriving with an
album UUID already on it has that adopted if the key is new.

**The key normalises incidental differences only**: case, spacing,
punctuation, Unicode forms and apostrophes (`Don't`, `Dont` and the curly
form are one album). It makes no judgement about editions: `Abbey Road` and
`Abbey Road (Super Deluxe Edition)` stay separate, because Spotify presents
them as two albums and merging them would put two track 1s in one record. A
name made only of punctuation (`+`, `÷`) keeps its characters. A file with
no album tag is its own album, keyed by its track UUID.

**Per library by construction.** Alex's copy and Kelly's copy of one album
are different files with different UUIDs.

**After a retag** (`registry.repoint`), the album keeps its UUID and only
the key moves, so its Navidrome identity — and album-level stars and plays —
survive a rename. If the new name is already in the registry, **the
incumbent wins** and the renamed files adopt its UUID. It has to be that way
round: only the newcomer's files are being rewritten, so choosing the
newcomer's UUID would split the established album rather than move it. This
is also how merging works: rename an album to an existing one and it joins
it.

---

## 8. Library

Everything you own, in the libraries you can see, read from Navidrome's
index every time. Strictly private: an administrator does not see another
person's list.

### Albums, Artists, Needs attention

**Albums** is a cover grid (or a dense list) with:

- **All / Albums / Singles** chips. A *single* is a folder holding one
  track — which is what every YouTube download is.
- **Sort**: recently added, artist, album, year, most played (your plays).
- **Status**: any, *Needs review*, *No MusicBrainz match*, *No ReplayGain*.
- **Search** across artist and album, which also lists up to eight **songs**
  whose titles match; a song opens its album.

Pages are 60 albums, rounded to fill the last grid row. The count line
covers the whole library: albums, tracks, how many need review, how many
have no MusicBrainz match. Album cards are flagged *Single*, *Cover* (bars
on the cover) or *Review*.

**Artists** groups by album artist, ignoring case, each with a mosaic of its
four most-played covers. An artist's page splits Albums from Singles, which
is where a record downloaded song by song shows up as a pile.

**Needs attention** collects:

- **Singles that belong together** — two or more single-track folders by
  one album artist. *Combine…* opens the combine dialog; *Not together*
  hides that exact group (remembered in your browser, so a new single
  arriving offers it again).
- **Singles already on an album** — a single whose song is on one of that
  artist's real albums. Not offered for combining, since that would put the
  song on the album twice; quarantine it instead.
- **Covers with bars** — letterboxed YouTube thumbnails; *Square all*.
- **Needs review** and **No ReplayGain**, with *Measure all*.

Finding barred covers opens the first track of every album, which takes
seconds on a Pi the first time. Each answer is remembered against the file's
modification time, so later checks are cheap — in memory, so a restart
starts over.

### What "matched" and "needs review" mean

- **No MusicBrainz match** — some track has no MusicBrainz recording id,
  read live from Navidrome every time. Nothing is stored, so an album leaves
  the list by gaining ids, and no flag can fall out of step. It is a
  statement about MusicBrainz, not a queue that empties: a doujin release or
  a bootleg can be tagged perfectly and never have an id.
- **Needs review** — unmatched *and* nobody has dealt with it here: applied a
  match, edited it, or pressed *Mark reviewed*. That is stored in
  `album_reviewed`, keyed on Navidrome's album id — which is derived from the
  album UUID, so the mark survives a rename. This is the part that can
  empty, and it is what the menu badge counts.

### One folder is one album

The Library groups tracks by folder, because the filer puts exactly one
album in one directory. Album-level actions — rename, match, cover,
ReplayGain, quarantine, combine — act on the folder. Editable albums are
`artist/album` folders inside the library, counted after resolving, so
`Artist/.` is the artist directory and refused; anything shallower or deeper
is read-only, except that ReplayGain measures a folder at any depth. Nothing
under `quarantine/` can be edited. Every request names a library id and a folder, never a path;
the server checks the library is yours, resolves the path, and refuses
anything that lands outside the library root (resolving, not filtering
`..`, so a symlink cannot walk out either).

Some older folders hold more than one album (57 found in September 2026).
They are flagged (*2 albums* on the card, a pill in the panel), and
folder-wide actions (rename, match, cover, combine) refuse them with the
albums named; ReplayGain skips them. Edit them track by track, or move one
album's tracks out first. See PLAN.md.

An album the inbox filed a track into within the quiet period is still
arriving, and edits to it are refused with a message until the download or
drop has finished. The app's own edits do not count: you can fix a title
and then its track number straight away.

### The album panel

Opening an album shows a panel beside the list: cover, title, artist (a link
to the artist page), year, track count, length, date added, your plays, the
library; status pills (MusicBrainz, review, ReplayGain, cover); and actions:

**Mark reviewed** · **Edit details** · **Fetch cover** · **Find matches** ·
**Missing tracks** · **More** (Measure ReplayGain, Combine with other albums…,
Quarantine album).

Then the tracks, read by their own query when the album opens; the list only
*counts* tracks, which keeps paging 2,800 albums cheap. The status bar
(`#library-status`), which reports everything a panel action starts, moves
into the panel while it is open.

### Missing tracks

**Missing tracks** compares the album with its full tracklist
(`albumcheck.py`) and lists every track, each marked **In library** or with
a **Download** button.

- **Which tracklist.** The MusicBrainz release the files are tagged with,
  when they carry one (about four albums in five): that is the edition
  *Find matches* chose, so it needs no guessing. Otherwise Spotify's copy of
  the album, chosen as the candidate sharing the most titles with the files.
  MusicBrainz is asked at most once a second, as it requires; if it does not
  answer, Spotify is used and the panel says why.
- **Editions.** An **Edition** menu offers the album's other releases - the
  rest of the MusicBrainz release group, one entry per distinct tracklist
  (twenty pressings of one CD are one choice), or Spotify's other
  candidates. A standard copy is complete, and missing its bonus tracks
  against the deluxe.
- **Held.** A track counts as held when one of the album's files is that
  MusicBrainz recording, or failing that has the same title once version
  notes ("Remastered 2011", "(Mono)") are set aside. One file answers for
  one track only. Files on no track of the edition are counted ("2 of your
  tracks are not on this edition"), which is the hint to try another.
- **Not offered.** A track already in the library under another album - a
  single, a compilation - says **In another album** rather than offering a
  second copy; moving it here is a track edit in that album. A track
  MusicBrainz has no title for ("[unknown]") says **Untitled**.
- **Downloading.** **Download** on a track, or **Download all N missing**,
  queues one job (`POST /api/library/album/missing/download`). Each track
  is looked up on Spotify for its ISRC, cover and exact length, then tagged
  with *this* album's artist and name and the edition's track and disc
  numbers, so it files into this album wherever Spotify spells it
  differently. A track Spotify does not have is still queued, from the
  reference's title, artist and length; the download searches YouTube
  Music, SoundCloud and Bandcamp with those alone.

Only on request: nothing checks albums in the background.

### Editing tracks

A track's **number, title and artist are inputs in its row** and save when
you leave a field you changed — no edit mode. Behind **More…**: disc number,
**Move this track** to another album (or out as its own single), and
**Quarantine this track**.

An edit rewrites only the tags you changed, keeping every other tag, the
art and both UUIDs. The file is then re-filed to the path its tags now
describe; moving to another album gives it that album's UUID (joining it if
it exists) while its own track UUID stays, so its stars and plays follow.
Emptied folders are removed up to the library root; a cover left behind
counts as empty once an identical copy has travelled with the tracks. Navidrome is asked to
scan, and the panel keeps what you typed rather than re-reading, since
Navidrome has not rescanned yet.

### Editing an album

**Edit details** takes an album artist and album name. Saving retags every
file in the folder, re-points the album's registry entry (section 7) and
re-files the tracks. A search under the fields finds existing albums as you
type; picking one fills in a name known to exist. That is the merge: no
special code path, just a rename onto an existing album, which joins it.
Before the search, one typo made a new album instead.

### Find matches (MusicBrainz, via beets)

**Find matches** asks beets what the album would match on MusicBrainz and
lists up to five candidates with their distance and what counts against
each ("held against it: tracks, year…"). **Use this** retags the files as
that release.

- It runs as a long operation, in a subprocess
  (`python -m app.beets_match`), because beets' configuration and plugins are
  process-wide. The candidate list is what beets' `quiet_fallback: skip`
  would otherwise throw away — beets is never allowed to guess.
- Applying runs beets with `move: no`, `copy: no`: it writes tags (and
  fetches and embeds art) and nothing else. Then the album UUID is
  re-pointed and the filer moves each file. One function decides where a
  track lives.
- The album is marked reviewed ("matched") only if beets actually applied
  the release; if it did not, the panel says the album is unchanged.
- Your lookups run one at a time. Asking about a second
  album while one is in flight is refused with a message, an answer is shown
  only under the album it was asked about, and **Use this** applies only a
  release that was offered for that album, to the person it was offered to.
  These offers are kept in memory, so after a restart you run *Find matches*
  again.
- Matching is one album at a time, by hand. This is also where Picard could
  replace beets later.

Turned off by `beets_enabled = false`.

### ReplayGain

**Measure ReplayGain** on one album, or **Measure all** for every album
without it, runs `rsgain easy` one folder at a time, as an operation with
progress and a **Stop** that takes effect between albums. A whole folder is
always measured together so album gain is consistent. One album failing
does not stop the run; Navidrome is asked to scan every 25 albums and at the
end. rsgain was checked on copies of real MP3, M4A and FLAC files: the UUID
tags survive.

### Covers

**Fetch cover** offers: the current cover squared (if it is not square), the
Cover Art Archive front cover (if the album has a MusicBrainz album id), and
Spotify's covers for a search on the artist and album. Choosing one embeds
it in every file — replacing existing pictures, touching no other tag — and
replaces any folder cover with one `cover.jpg`. Only `https` images from
Spotify's image host or the Cover Art Archive can be chosen.

**Squaring** (`covers.square`) is applied to every cover, including
downloads: a square JPEG passes through untouched; a 4:3 image whose top
and bottom bands are near-black has the letterbox removed; anything else is
centre-cropped to a square JPEG. YouTube thumbnails arrive as letterboxed
4:3, which is what *Covers with bars* finds and *Square all* fixes.

### Combine into album

**Select** turns on select mode: tap albums in the grid, or tick tracks inside
an open album; the selection survives searching and switching tabs. The bar
offers **Combine into album…**, **Square covers** and **Mark reviewed** for
everything selected.

The combine dialog asks three things:

1. **Which album they end up in** — one of the selected albums (it keeps its
   name, UUID, stars and plays) or a new name (typing an existing album's
   name joins it). If the selection spans several artists, *Various
   Artists* is offered; only the album artist changes, so each track keeps
   its own artist.
2. **Order** — drag or use the arrows; optionally number them 1–N. A song
   appearing twice is pointed out.
3. **Cover** — any selected album's, or Spotify's.

If the name is empty, Spotify suggests one: each song is searched, each
votes for the full albums it appears on (singles and EPs do not vote), and
a one-vote answer to a several-song question is no answer. It only ever
fills an empty field.

Combining is the ordinary retag in a fixed order: the kept album first; every
other album renamed onto it (adopting its UUID); loose tracks moved onto it;
then renumbering and the chosen cover on every file — the cover read before
anything moved. Track UUIDs are never touched. It runs as an operation with
per-file progress, since three albums is dozens of retags, and confirms in
its own dialog instead of offering an undo.

### Quarantine

Four ways to take music out of the library, all confirming first:
**Quarantine album** (the album's More menu), **Quarantine this track**
(a track's More…), **Quarantine…** on the select-mode bar (every selected
album and loose track), and **Remove** on a song in the Library's search
results. Each moves the files into `quarantine/` (section 9),
recorded in the same ledger, where the **Quarantine page** (section 9a)
can put them back or delete them for good. The files to move are taken
from Navidrome's index for that folder, never from the request. Stars are
not migrated - there is no other copy to move them to - and they come back
with the file on a restore. It is refused while the inbox is still filing
into that album.

When the last track leaves a folder (by quarantine or a resolved
duplicate), its folder cover follows the tracks to the same place inside
the quarantine, so the album can be put back whole, and the emptied folder
is removed with any parents it emptied. Anything else in it, such as a cue
sheet or a rip log, keeps the folder.

### Genres and Rescan

**Genres** shows every genre string in your libraries with its track count,
exactly as tagged — deliberately not merged or case-folded, so spelling
variants ("Electronic" beside "electronic") are visible for a later merge.
Untagged tracks are counted apart. Only a track's first genre tag counts.

**Rescan** asks Navidrome for a scan now, with the service account.

---

## 9. Duplicates

Copies of the same recording, **always within one library**. (An early
version grouped across libraries; 74 of 271 groups spanned two, and a bulk
resolve would have removed Kelly's music.)

### How copies are grouped

Only live tracks in your libraries are considered. Two ways:

- **Same MusicBrainz recording id.**
- **Same track artist and normalised title** — edition words (remaster,
  deluxe, bonus track, album/single version, explicit, clean, mono, stereo,
  radio edit, extended) and "feat." clauses removed, then everything but
  letters and digits.

A title group that adds nothing to a MusicBrainz group is dropped, and a
group whose lengths differ by more than five seconds is dropped whole — those
are different mixes. Each copy's own title is shown, so two different
collaborations that normalise alike are visibly different.

A group is **confident** only if it was found by MusicBrainz id, every copy
is on the same album, lengths agree within a second, and one copy is clearly
better: lossless over lossy, or at least 10% more bitrate. Confident groups
are listed first.

**The copy kept** is pre-selected by: a copy another user has starred, rated
or played (that cannot be moved), then lossless, bitrate, file size, and having a
MusicBrainz id.

### Resolving a group

**Keep selected, remove the rest** confirms, listing what stays and what
goes. Then:

1. **Refused before anything moves** if a copy being removed carries someone
   else's star, rating or plays — the app can only act as you. The confirm
   also says how many of your own plays Navidrome counts on the removed
   copies; those cannot be moved, though Home's history keeps them.
2. **Stars and ratings move first, as you**, through the Subsonic API: if a
   removed copy is starred and the kept one is not, the kept one is starred;
   the highest rating is carried over. If that fails, nothing is removed.
   (Migrating as an administrator would create an invisible admin star and
   quarantine the real one.)
3. Each other copy is **moved** to `<library>/quarantine/<its
   original path>`, on the same filesystem, numbered on collision.
4. Each move is recorded in `duplicate_quarantined`: from, to, the copy
   kept, who decided, when.
5. Navidrome is asked to scan.

**Keep both** records the decision against a hash of the copies' track ids,
so it survives regrouping, and against you: someone else sharing the library
still sees the group. (Decisions from before October 2026 recorded nobody
and apply to everyone.) **Resolve MusicBrainz matches** previews every
confident group, confirms, and resolves exactly those groups with their
pre-selected copy, as an operation; a group that appeared or changed since
the preview is left for the next look. It is refused while a download or
drop is still filing into your library.

Do not bulk-resolve while music is still being filed: importing is what
creates duplicates, so a list taken beforehand is stale by the end. About 50
groups are a single beside its own album rather than true duplicates.

### The quarantine folder

`quarantine/` sits inside each library root so moving into it is a
rename on one filesystem. It was called `duplicates-removed/` until
2026-10-09, when it stopped being only for duplicates; on start-up the app
renames an old one (merging into `quarantine/` if both exist) and points
the ledger's paths at the new place. (It once sat outside every volume, so a
"quarantined" file was copied into the container's writable layer, the
original deleted, and the copy destroyed by the next update.) It holds an
**empty** `.ndignore`, which keeps Navidrome from scanning it — a non-empty
one is read as a list of patterns and lets everything else back in, so the
app rewrites it if it is not empty — and a `README.txt` saying all this.

What is in it is on the Quarantine page, below.

---

## 9a. Quarantine

Everything set aside, from either road - removed by hand in the Library,
or the losing copy of a duplicate - an album to a card, newest first
(`quarantine.py`, `quarantine.js`). The disk is read and the ledger joined
on, so a file an older version set aside, with no record, is listed too,
named from its own tags; one buried under
`quarantine/duplicates-removed/…` by the old .ndignore bug is listed under
the folder it really came from.

- **Filters and search:** All, Removed by hand, Duplicates, No record; and
  a search over artist, album, title and path.
- **Restore** (a track, or **Restore all** for an album) moves the file
  back to the path it came from. That is how Navidrome knows it - its track
  UUID never changed - so its stars and plays return on the next scan,
  which is asked for. If the path is taken (often by the copy kept in its
  place), the file is filed by its tags instead, under a new name if it
  must be. The album's folder cover comes back with its last track.
  Restoring the loser of a duplicate pair asks first: it is a duplicate
  again, and will be back on the Duplicates page.
- **Delete…** (a track, or **Delete all…**) removes the file from disk
  after a confirmation. **Delete everything older than** 30 days, 90 days
  or a year does the same for everything set aside before then. The
  ledger row stays, stamped `deleted_at`, as the record that it existed.
- Every path arrives as `<library id>:<path inside the quarantine>` and is
  acted on only once it resolves to a file inside the quarantine of a
  library you can see. Restores and deletes hold the libraries' folder
  locks, so no edit, match or ReplayGain run meets a file mid-move.

---

## 10. Health

"Every number here should be zero." Checks against Navidrome's database and
a direct read of the files, limited to your libraries. Rows marked
*secondary* are dimmed and never count toward the badge: things that can
recur but rarely do, and facts that are status rather than health.

| Section | Check | Counts toward the badge |
|---|---|---|
| Identity | Tracks with no UUID | yes |
| | Stars and ratings pointing nowhere (yours) | yes |
| | Duplicate UUIDs | yes |
| | Stamped but not yet scanned | yes |
| Libraries | Each library's UUID coverage | secondary |
| | Files Navidrome can no longer find | no |
| | Last scan | secondary |
| | Duplicate recordings to review | yes |
| Metadata | Tracks with no ReplayGain | no |
| | Tracks with no MusicBrainz id | no |
| On disk | Audio files | secondary |
| | Directories with two album UUIDs | secondary |
| | Album UUIDs spread across directories | secondary |
| | Unreadable files | yes |
| System | Uptime | secondary |
| | Free space (warn under 10%, fail under 5%) | yes |

- **The disk wins.** Where Navidrome's index and the files answer the same
  question, the files' answer is used: the index can be stale.
- **The disk audit** walks each library reading identity tags directly,
  skipping what Navidrome skips: the quarantine, and any directory holding
  an empty `.ndignore` (`walk.py`, shared with `survey`, `unfuse` and
  `backfill`). It
  runs in the background, re-run when older than six hours, and on demand
  with **Re-read files**. Simultaneous requests share one walk.
- **Stamped but not yet scanned** is the only check that can tell "never
  stamped" from "stamped, but Navidrome's incremental scan skipped the file"
  (a tool that restores a file's modification time after tagging causes the
  second). They need different fixes.
- **Duplicate recordings to review** runs the real duplicate finder (a
  fraction of a second) rather than an approximation in SQL, which would
  disagree with the Duplicates panel.
- `.wav` and `.aiff` files are excluded from tag checks; they cannot carry
  the tags.
- A failing query replaces its section with "Checks unavailable"; the rest
  still render.
- The checks that read Navidrome are kept for up to a minute per person,
  and sooner if the tracks, your plays and ratings, a duplicate decision or
  the disk audit change. Disk, uptime and free space are read every time.

---

## 11. Playlists

**Smart playlist rules.** Navidrome plays and renames smart playlists but
will only take their rules from a `.nsp` file, so this is where they are
written. Ordinary playlists are deliberately left alone — Navidrome edits
those well, and a copy here would be worse.

You see the smart playlists you own. The editor is a flat form: match
**all** or **any** of a list of conditions, a sort and direction, a limit,
and (if you have more than one) which libraries to draw from.

| Field kind | Fields | Operators |
|---|---|---|
| text | title, album, artist, album artist, genre, comment, file path, file type | contains, does not contain, is, is not, starts with, ends with |
| number | year, rating, play count, duration, bitrate, BPM, track number, disc number | greater than, less than, is, is not |
| yes/no | starred, compilation | is |
| date | date added, last played, date starred | in the last / not in the last *n* days, before, after |

Sorts: date added, date starred, last played, play count, rating, year,
title, album, artist, duration, random.

**Translation happens on the server.** Navidrome's nested operator shape
never reaches the browser. Rules that cannot be shown as a flat list — a
nested group, both *all* and *any* at the top level, an unknown field or
operator — are listed as "Written by hand" with no Edit button, rather than
flattened and silently changed. A smart playlist quietly matching the wrong
thing is worse than one the app declines to open.

**Library scoping.** Navidrome evaluates a smart playlist against every track
on the server, whoever owns it: Kelly's "play count > −1" matched ~7,000
tracks against her 437. So every save adds `{"is": {"library_id": …}}` for
the libraries chosen (all of yours if none are ticked). It uses `is`, not
`contains`, which compiles to a text match where `1` is a substring of `10`.
An *any* playlist is wrapped so the scope still applies to every match. A
playlist saved before this shows "Draws from every library on the server"
until it is saved again.

Saving validates every condition (a day count for *in the last*, a real date
for *before* and *after*), goes through Navidrome's API as you, and
reports how many tracks Navidrome says it now matches. Deleting removes only
the playlist, never tracks.

---

## 12. Settings

Visible to everyone, editable only by a Navidrome administrator, because it
holds the Spotify credentials and the Navidrome service password and decides
where every library's music goes. Secrets are never sent to the browser; a
blank secret field leaves the value alone. Changes apply immediately (a new
Spotify credential resets the Spotify client) and are written to
`config.toml`.

Fields: Navidrome URL, service account and password; Spotify client id and
secret; simultaneous downloads, bitrate, attempts per track, pause between
downloads; AcoustID key. Every key, including the ones only settable in the
file or environment, is listed in [SETUP.md](SETUP.md#every-setting).
A key set by an environment variable wins over the file, is shown locked
in the panel with the variable's name, and is never written to
`config.toml`.

---

## 13. Background work and long operations

Three loops run for the life of the process:

| Loop | Every | Does |
|---|---|---|
| inbox | 15 s | files whatever has settled in any inbox |
| play counts | 5 min | reads Navidrome's play counts, then warms Home for whoever played |
| disk audit | 10 min | re-reads any library whose audit is older than 6 h |

**Operations** (`operations.py`) are the long jobs that are not downloads:
finding candidates, applying a match, combining, ReplayGain, the disk audit.
Each runs off the request in a worker thread, one at a time per person and
name, with a status only its owner can ask for (`/api/operations` returns
only yours) and progress pushed over the WebSocket to that person. Starting
one of yours that is already running reports the one in flight instead of
starting a second; someone else's never blocks yours, so two people can
each run ReplayGain at once. On reload, the page picks up any operation of
yours still running.

Messages about work a panel started appear in that panel, not in a banner
over the whole page; switching view clears the page-wide banner.

---

## 14. API reference

All routes need a session unless marked **open**; **admin** means a Navidrome
administrator.

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | **open**; container healthcheck |
| POST | `/api/auth/login` · `/api/auth/logout` | **open**; sign in / out |
| GET | `/api/auth/me` | **open**; who is signed in |
| GET | `/api/status` | whether Spotify is configured |
| GET · PUT | `/api/settings` | read / change settings (**admin** to see values or change) |
| GET | `/api/overview` | Home's dashboard |
| GET | `/api/playcounts` | collector status (admin) |
| GET | `/api/playcounts/top` | the statistics section for a range |
| POST | `/api/playcounts/snapshot` | **admin**; read play counts now |
| GET | `/api/search` | Spotify search with "in library" marks |
| GET | `/api/albums/{id}` · `/api/artists/{id}/albums` | Spotify album / discography |
| POST · GET | `/api/jobs` | queue a link / list your jobs |
| GET · DELETE | `/api/jobs/{id}` | one job / stop and forget it |
| POST | `/api/jobs/{id}/cancel` · `/retry` | cancel / retry failed tracks |
| POST | `/api/inbox/upload` · `/api/inbox/upload/finish` | Drop page |
| GET | `/api/library` | albums, filtered, sorted, searched |
| GET | `/api/library/artists` · `/attention` · `/attention/covers` · `/genres` | Library tabs |
| GET | `/api/library/album` · `/api/library/art` | one album's tracks / cover image |
| POST | `/api/library/track/edit` · `/album/edit` · `/reviewed` | editing |
| POST | `/api/library/match` · `/match/apply` | MusicBrainz candidates / apply |
| POST | `/api/library/cover/candidates` · `/cover/apply` | covers |
| POST | `/api/library/combine` · `/combine/guess` | combine / name suggestion |
| POST | `/api/library/replaygain` · `/replaygain/stop` | ReplayGain |
| POST | `/api/library/quarantine` · `/track/quarantine` | set aside |
| GET | `/api/quarantine` | everything set aside, by album |
| POST | `/api/quarantine/restore` · `/delete` · `/empty` | put back / delete for good / delete older than N days |
| POST | `/api/library/rescan` | ask Navidrome to scan |
| GET | `/api/operations` | long operations' status |
| GET | `/api/health` · POST `/api/health/audit` | Health / re-read files |
| GET | `/api/duplicates` | duplicate groups |
| POST | `/api/duplicates/resolve` · `/dismiss` · `/auto` · `/auto/apply` | resolve / keep both / preview confident / resolve exactly those |
| GET · POST | `/api/playlists` | list / create smart playlists |
| PUT · DELETE | `/api/playlists/{id}` | save / delete |
| WS | `/ws` | job and operation events, to their owner only |

---

## 15. Gotchas learned the hard way

Each of these cost real time or caused a real bug.

**Navidrome**

- `computePID` hashes `""` for a missing tag; a single-field PID chain
  collapses every untagged file into one track.
- Vanished directories are marked missing on the **folder** row, not on
  `media_file`. Queries must join `folder` or they count deleted files.
- A tool that restores a file's modification time after tagging means an
  *incremental* scan never re-reads it; a **full** scan is needed. (The
  filer and Library edits do not restore it, so their scans see the change.)
- Smart playlists ignore library access; scope them in the rule, with `is`.
- Migrate stars as the owning user, never as admin.
- Navidrome runs SQLite in WAL mode: mount its directory, not the file.

**beets**

- Item paths are stored relative to `directory`; resolving against the
  wrong root silently does nothing.
- Output is colourised even when redirected: `ui: color: no`, and strip
  ANSI anyway.
- Since 2.x, `musicbrainz` must be named in `plugins`.
- A YAML plain scalar cannot start with `%`; quote `%if{}` path templates.
- An invalid field name in a path template (`$album_artist_no_feat`) files
  albums into a folder literally called that.

**Audio files**

- MP4 freeform atoms must use the `com.apple.iTunes` namespace.
- `.wav` and `.aiff` cannot carry these tags.

**Container boundaries: a path that resolves is not a path that persists.**
Navidrome reports library paths from its own database, and this is a
different container. A Linux container will create a missing directory in
its own writable layer and let you write to it; everything succeeds, and the
next `docker compose pull` destroys it. That happened twice — to a library
missing its mount, and to the quarantine folder. Before writing anywhere
outside `/config` or `/downloads`, check the destination is on a mount, not
merely that the path resolves. `workspace.require_mounted` does this.

**Async jobs**

- Cancel a job's task *and await it* before removing its files.
- The download gate is process-wide, so a task that never releases a slot
  starves every later job of every user. Orphaned tasks are an outage, not a
  tidiness problem.

**Browser caching**

- A response with no `Cache-Control` is still cached, for a lifetime the
  browser invents. `/static` is `no-cache` (revalidate) and the shell stamps
  `?v=<hash>` on asset URLs, because headers cannot rescue a browser already
  holding a stale copy — only a URL it has never seen can.

**CSS and Safari**

- Every size, weight, gap and colour comes from a token in `:root`; tests
  fail the build on a raw value.
- Form fields must be at least 16px, or iOS Safari zooms on focus and does
  not zoom back.
- `[hidden]` loses to any class that sets `display`; it is restated with
  `!important` at the top of the stylesheet.
- `overflow-x: hidden` on `html`/`body` breaks `position: sticky` (and on
  WebKit loosens `position: fixed`); `clip` breaks sticky in Chromium. Use
  `overflow-wrap` for long strings.
- `backdrop-filter` makes an element the containing block for `fixed`
  descendants; the header is deliberately unfiltered.
- `env(safe-area-inset-*)`: the header pads for the notch, the full-height
  menu for both ends.

**The recurring bug in this codebase is silent failure** — a bare
`suppress`, an `except Exception: return []`, a flag argparse accepts and
nothing reads, a check whose SQL never ran, a day that writes no rows and so
never counts as done. Prefer a fix that makes the failure visible over one
that makes it less likely.
