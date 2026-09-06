# Download Center — how the code works

A reference for the codebase as it stands on 2026-09-06. For what it is
*for* and where it is going, see [PLAN.md](PLAN.md); to pick up work, see
[HANDOFF.md](HANDOFF.md).

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
                              └──filesystem──▶  staging/ ─▶ music/, kelly/
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
| `config/state.db` | What has already been downloaded; dismissed duplicate groups | Read/write, ours alone |
| `config/beets/<workspace>/library.db` | Beets' index of one person's filed music | One per workspace |
| The audio files | `navidrome_uuid`, MusicBrainz ids, all tags | The truth. Everything else is a cache |

`state.db` has exactly two tables — `ledger` (source_id, isrc, title,
artist, album, file_path, completed_at) and `duplicate_dismissed`
(group_key, note, decided_at). It deliberately holds no user table, no
library table and no copy of anything Navidrome knows.

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
- `staging/<key>/` holds `albums/`, `singles/`, `.incomplete/` and a
  four-line `.owner` marker naming the user, library name, library path and
  library id.
- `config/beets/<key>/` holds that person's `config.yaml` and `library.db`.

The `.owner` marker exists because the staging sweep runs on a timer with
nobody signed in — the folder itself has to say who its files belong to.
`prepare()` refuses if the marker names a different user, since two
usernames can slug to the same directory name.

**Why beets is split per person:** beets stores item paths *relative to* its
`directory`. One `library.db` genuinely cannot describe two roots.

---

## The download pipeline

```
URL ─▶ spotify.py / generic.py ─▶ resolved job (list of tracks)
                                        │
                                   worker.py
                                        │
        ledger check ─▶ matcher.py ─▶ downloader.py ─▶ tagger.py
                                        │
                                   staging.py
                          (.incomplete/ ─rename─▶ albums/ or singles/)
                                        │
                              beets_runner.py sweep
                                        │
                          beets moves it into the library
                                        │
                                    stamp.py
                          (writes navidrome_uuid to filed files)
                                        │
                              navidrome.trigger_scan()
```

- **`spotify.py`** resolves a Spotify link to tracks with full metadata, and
  backs the Browse tab's search.
- **`generic.py`** handles anything else yt-dlp understands.
- **`matcher.py`** picks the YouTube Music recording corresponding to a
  Spotify track, scored on title, artist and duration.
- **`downloader.py`** fetches with yt-dlp and converts to MP3.
- **`tagger.py`** writes Spotify metadata as seed data for beets.
- **`staging.py`** assembles albums in `.incomplete/` and *renames* them into
  place — a rename, so beets never sees a half-written album. `settled`
  requires a folder to be quiet for `staging_quiet_seconds` before import.
- **`beets_runner.py`** generates each workspace's config from a template
  (placeholder substitution, **not** `str.format` — beets path templates are
  full of braces) and runs the import.
- **`stamp.py`** assigns identity tags *after* beets has filed a file, not at
  download time, because beets moves and rewrites files.

---

## Companion features

- **`health.py`** (522 lines) — checks against Navidrome's database and the
  disk audit. Scoped to the signed-in user's libraries via `_live_clause`,
  with one exception: `orphan_annotations` counts every user's dangling
  stars, because the rows it looks at have no library to scope to.
- **`diskaudit.py`** — walks the library reading tags directly, because
  Navidrome's index can be stale or wrong. Runs in the background and
  caches.
- **`duplicates.py`** — groups copies of the same recording by MusicBrainz
  id and by normalised title, **always within one library**. Ranks copies,
  migrates stars and ratings onto the keeper, and *quarantines* losers to
  `duplicates-removed/`. Nothing is deleted.
- **`playlists.py`** — smart playlist rules. Translates between Navidrome's
  nested operator shape and a flat form the browser can render. Rules it
  cannot represent are marked unsupported rather than flattened.

---

## HTTP layer

`main.py` (939 lines — it wants splitting) holds every route.

- A `require_session` middleware gates all `/api/*` except the three auth
  paths. `/healthz` is deliberately ungated so the container healthcheck
  works.
- `current_session` is the normal dependency; `admin_session` gates settings
  writes.
- Jobs are scoped per owner, and WebSocket events fan out only to the
  session that owns the job.
- The shell is served `no-store`; a cached pre-auth page once made the app
  look like it would not sign in.

---

## Front end

`app.js` is 1,215 lines in one global scope, organised by panel: queue,
browse, staging, health, duplicates, playlists. Panels are shown by
toggling `hidden` on `#view-<name>` sections, driven off the tab buttons
themselves rather than a hand-kept list — an earlier version named a view
that no longer existed and the resulting throw hid every panel at once.

`style.css` is token-based: colours, spacing scale, radii and shadow all
come from `:root`, with one button in three weights (filled, outlined,
bare). A `@media (max-width: 720px)` block turns the tabs into a fixed
bottom bar. All of this is scheduled to change with the burger rewrite.

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

**CSS**
- `[hidden]` is a user-agent rule and loses to any class setting `display`.
  Stated once, `!important`, at the top of the stylesheet.
- `overflow-x: hidden` on `html`/`body` makes them scroll containers, which
  breaks `position: sticky` for descendants — and on WebKit knocks
  `position: fixed` loose. `clip` breaks sticky in Chromium too. Do not set
  it; handle long strings with `overflow-wrap`.
- `backdrop-filter` makes an element a containing block for `position: fixed`
  descendants. Blurring the header pinned the bottom tab bar inside it.
- On iOS a fixed bottom bar sits at the *layout* viewport bottom, which is
  not the screen bottom. Extend the element's own box past it; a
  same-coloured `box-shadow` is not painted there.

**This environment**
- `python` is not on PATH. Use `.venv/Scripts/python.exe`.
- Some bash heredocs fail in this harness; write files with the editor tool
  instead.
- Files are CRLF; git warns on every commit. Preserve line endings when
  rewriting a file programmatically.
