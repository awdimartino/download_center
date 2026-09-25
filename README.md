# Download Center

A companion to [Navidrome](https://www.navidrome.org/): the things it has no
answer for. Download music, edit smart playlist rules, audit library health,
and resolve duplicate copies — from a phone or a computer.

It runs as one container beside Navidrome, sharing the music volumes and
reading Navidrome's database. Navidrome stays the authority on accounts,
libraries, stars and ratings; nothing here keeps a second copy of any of it.

```
paste a link -> download -> seed tags -> beets -> tagged library -> Navidrome
```

The tags this writes are deliberately provisional. They exist so beets has
something accurate to match against; beets then decides the canonical release,
writes the MusicBrainz identifiers, fetches cover art, and files the result
into the tree defined in its own config. Matching a whole album at once, on
track count, ordering, durations and artist together, is far more reliable
than any per-track identifier lookup.

## Accounts

**You sign in with your Navidrome account.** There is no user list here and no
setup per person — Navidrome already knows who exists and which libraries each
account may see, so a new user signs in once and everything follows from that.

Everything is scoped to whoever is signed in:

| | |
|---|---|
| Where a download lands | the library that account can see in Navidrome |
| Inbox and scratch space | `<workspaces>/<username>-<library id>/inbox/` |
| beets config and index | `config/beets/<username>-<library id>/` |
| Whose stars a duplicate carries | theirs |
| Who owns a new playlist | them |

Credentials are never stored. A session lives in memory for a fortnight, and
restarting the container signs everyone out.

Changing the Settings panel requires a **Navidrome administrator** account,
because those settings hold the Spotify credentials and the Navidrome service
password and decide where every library lives.

## The tabs

**Queue** — paste a link, watch it download. Job state is in memory; see
*Behaviour worth knowing* below.

**Browse** — search Spotify from inside the app, so you never have to go and
copy a link. Albums, tracks or artists; open an album for its track listing or
an artist for their discography with duplicate reissues collapsed. Tracks
your library already holds are marked, so you can tell what you are missing
from a record before queuing it. It is a note, not a gate — nothing is
refused because of it.

**Review** — music that is in your library and playing, but that has never
been matched against MusicBrainz. Nothing here is waiting for permission; the
row only says nobody has confirmed it. *Find matches* asks beets what one
album would match against, and you pick from the list.

**Health** — a list of numbers that should be zero: tracks with no UUID,
duplicate UUIDs, stars pointing at files that no longer exist, unreadable
files, ReplayGain coverage, free space. Everything is scoped to the libraries
your account can see. *Re-read files* walks the library reading tags directly,
because Navidrome's index can be stale.

**Duplicates** — copies of the same recording, grouped by MusicBrainz
recording id or by normalised title and runtime, **always within one library**.
Nothing is deleted: the copy you drop moves to a `duplicates-removed/`
directory, and any star or rating on it is migrated onto the copy you keep
first. A group whose copies carry someone else's star is refused rather than
resolved, since acting as another account is not possible.

**Playlists** — smart playlist rules. Navidrome plays and renames these
happily but will only *write* rules from a `.nsp` file, so this is where they
get edited. Rules that cannot be shown as a flat list of conditions are marked
uneditable rather than flattened, because a smart playlist quietly matching
the wrong thing is worse than one the app declines to open. Ordinary playlists
are deliberately left alone — Navidrome edits those well.

## What it handles

**Spotify** track, album, and playlist links. Metadata comes from the Spotify
API and the audio is located on YouTube Music by matching on runtime, title
and artist. Artist links are rejected deliberately, since a discography is
rarely what you meant to queue.

**Direct links** to anything yt-dlp supports — YouTube, YouTube Music,
SoundCloud, Bandcamp and hundreds of others — including playlists. These skip
the matching stage entirely, because the URL already names the audio.

Tag quality on direct links depends on what the site publishes. YouTube Music,
Bandcamp and `- Topic` channels expose real track, artist and album fields and
tag cleanly. A plain upload offers only a video title, so artist and title are
guessed by splitting on " - ", which follows the usual convention but will get
it backwards on a title like "Game Over - Free Metal Instrumental". Direct
links are always staged as singles, since there is no track count to prove a
release is complete.

## Tagging and filing

On first use a beets config is written to
`config/beets/<username>-<library id>/config.yaml` and never overwritten
afterwards. There is one per person, because beets stores item paths *relative
to* its `directory` — a single `library.db` genuinely cannot describe two
library roots.

Beets no longer files anything on its own. A download goes into the library
immediately, tagged from Spotify; beets is consulted by hand, one album at a
time, from the Review tab. It ran unattended once and refused 82% of what it
was given — almost all of it for mechanical reasons that had nothing to do
with the music.

Two things in the generated config are load-bearing, and worth understanding
before editing it:

- **No `%aunique{}` and no year in the album directory.** Both would file two
  releases of one record into separate folders. One directory is one album is
  what lets a later arrival inherit the album UUID its siblings already share
  instead of founding a second copy. Navidrome sorts on the year tag, not the
  folder name, so nothing is lost.
- **`plugins: musicbrainz ...` must name `musicbrainz` explicitly.** Since
  beets 2.x it is a plugin, and naming any plugins replaces the default list
  rather than adding to it. Omit it and album matching is disabled entirely —
  every import silently skips with "Evaluating 0 candidates".

`fpcalc` is in the image, so acoustic fingerprinting can be enabled by adding
`chroma` to the plugins line — useful if you want identification by audio
rather than by tags.

Set `beets_enabled = false` to turn matching off entirely. Downloads still
land in the library; the Review tab simply stops offering to identify them.

## Track identity

Every filed track carries a `navidrome_uuid` tag, written *before* the file
is moved into place, so Navidrome never sees a track without one. Navidrome is configured to derive its persistent
track id from it:

```
PID.Track = 'navidrome_uuid|musicbrainz_trackid|albumid,discnumber,tracknumber,title'
```

That is what makes stars, ratings and play history survive re-tagging, moving,
renaming and a rebuilt database.

**Never configure a single-field PID chain.** Navidrome hashes the empty
string for a missing tag, so a bare `navidrome_uuid` would collapse every
untagged file into one track. Always keep the fallbacks after it.

Stamping restores the file's mtime, since nothing about the audio changed.
The cost is that an *incremental* scan will not re-read those files — after a
tag change, run a **full** scan.

## Where files go

One road into the library, for everything. A download and a file dragged in
over the network share take the same path, and one function does the filing —
which is the point, because the album-versus-single routing and the regrouping
pass that used to exist were all consequences of those being two roads.

```
<workspaces>/
  alex-1/                     one directory per user and library
    .owner                    who this belongs to, and where it files to
    inbox/                    drop music here; it does not stay
      .incomplete/            a download is built here, hidden from the watcher
```

A download is built in `.incomplete/`, tagged from Spotify, then moved into
the inbox and filed on the spot. Anything else is picked up by a watcher that
looks every 15 seconds and files whatever has stopped changing for
`inbox_quiet_seconds`. At rest the inbox is empty.

That quiet period is also the safety net: a rename keeps a file's mtime, so a
download the application died on is sitting in the inbox and gets filed on the
next start rather than being lost.

The `.owner` marker exists because the watcher runs with nobody signed in — a
file dropped in by hand has no session attached, so the folder itself has to
say whose it is.

Each file is filed by its **own tags**, to a path that is then frozen:

```
$albumartist/$album/$disc-$track - $title.ext
```

The disc prefix appears only on a multi-disc release. A track with no album
goes to `$artist/Unknown Album/`; a file with no readable tags at all goes to
`Unknown Artist/Unknown Album/` and shows up in the Review tab. Nothing is
held outside the library waiting to be identified.

After that no automatic process moves a file. Navidrome identifies a track by
its UUID and groups albums by tag, never by path, so a path that drifts from
the tags is cosmetic. Only a retag you confirm in the Review tab moves
anything.

### Which album a track belongs to

Album membership is a lookup, not a guess. `config/state.db` maps a
normalised `(album artist, album)` key to an album UUID: the first track of a
record mints one, and every later track — same job or months later — finds it.

The key normalises case, spacing, punctuation and unicode spelling only. It
does **not** treat editions as the same record: Spotify presents `Abbey Road`
and `Abbey Road (Super Deluxe Edition)` as two albums with two ids, and
merging them would put two track 1s inside one album.

## Setup

```bash
curl -O https://raw.githubusercontent.com/awdimartino/download_center/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/awdimartino/download_center/main/.env.example
nano .env                            # set DC_IMAGE, paths, PUID/PGID
docker compose up -d
```

Then open <http://localhost:8000> and sign in with a Navidrome account.

**Every library needs a bind mount here, at the same path Navidrome uses.**
Navidrome reports where a library lives from its own database, and this is a
different container. Add a library there and forget the mount here and the
path is valid for Navidrome and absent for this — at which point beets is
configured with a `directory:` that exists only inside the container. It
gets created in the container's writable layer, the music is filed into it,
the import reports success, and the next `docker compose pull` destroys the
lot.

This is now refused with a message naming the path, rather than discovered
later. Mounting both containers at the same path is what makes a library
path read from Navidrome's database directly usable here with no mapping to
maintain.

**Mount Navidrome's database.** It is commented out in `docker-compose.yml`
and it is not optional: which libraries an account may see is read from that
file, so without it every user signs in with no library, no download has
anywhere to go, and the Health panel has nothing to report. Mount it
**read-only** — Navidrome owns that file and caches from it, and nothing here
ever writes to it.

```yaml
    volumes:
      - /path/to/navidrome/navidrome.db:/navidrome/navidrome.db:ro
```

Spotify credentials go in the Settings panel, or in `.env` beforehand. Get
them at <https://developer.spotify.com/dashboard>; any redirect URI will do,
as this only uses the client-credentials flow.

Set `navidrome_url`, `navidrome_user` and `navidrome_password` in Settings to
have Navidrome scan as soon as something is filed. Without them new music
simply waits for Navidrome's own schedule. The account must be an
administrator, because Navidrome restricts scanning to admins.

All host paths come from `.env`, so `docker-compose.yml` never needs editing:

| Variable | Meaning |
|---|---|
| `DC_IMAGE` | the published image to run |
| `LIBRARY_DIR` | the tagged library beets files into, mounted at `/music` |
| `STAGING_DIR` | workspace root, mounted at `/downloads` |
| `CONFIG_DIR` | where `config.toml` and `state.db` live, mounted at `/config` |
| `PUID` / `PGID` | user that should own the written files |
| `PORT` | host port to serve on |
| `TZ` | container timezone |

`PUID`/`PGID` must match the user that owns your library directory, or the
files written here will not be readable by whatever serves them. Find it with
`stat -c '%u %g' /mnt/music`.

There is no TLS and the session cookie is not marked `secure`, so serve this
on a trusted network or put it behind a reverse proxy — sign-in posts a
Navidrome password.

## Deploying to a Raspberry Pi

**Use a 64-bit OS.** Check first:

```bash
uname -m        # aarch64 = good;  armv7l = see below
```

On 32-bit `armv7l`, rapidfuzz, uvloop and httptools have no prebuilt wheels
and must compile from C++ source on the Pi, which is slow and can exhaust
memory. Reinstall with Raspberry Pi OS (64-bit) rather than fight it.

Images are published to GitHub Container Registry for `linux/amd64` and
`linux/arm64`, so the same tag works on a PC or a Pi. **There is no need for a
checkout or a build on the Pi** — pushing to `main` builds the arm64 image in
CI, and deploying is a pull and a restart:

```bash
docker compose pull download-center
docker compose up -d download-center
```

The Pi only needs `docker-compose.yml`, `.env`, and the `config/` directory.
`restart: unless-stopped` brings it back after a reboot.

Check it came up:

```bash
docker compose logs -f
docker inspect download-center --format '{{.State.Health.Status}}'
```

To build from source instead, clone the repo and swap `image:` for `build: .`
in `docker-compose.yml`.

## Configuration

Set in `config/config.toml`, or from the Settings panel in the UI (which needs
a Navidrome administrator). Every key can be overridden by an environment
variable, which wins over the file — including over anything the Settings
panel writes.

| Key | Default | | |
|---|---|---|---|
| `spotify_client_id` | | `DC_SPOTIFY_CLIENT_ID` | |
| `spotify_client_secret` | | `DC_SPOTIFY_CLIENT_SECRET` | |
| `concurrency` | `3` | `DC_CONCURRENCY` | simultaneous downloads, per job |
| `audio_bitrate` | `320` | `DC_AUDIO_BITRATE` | MP3 kbps |
| `max_attempts` | `3` | `DC_MAX_ATTEMPTS` | tries per track before failing |
| `rate_limit_sleep` | `2.0` | `DC_RATE_LIMIT_SLEEP` | seconds between downloads |
| `beets_enabled` | `true` | `DC_BEETS_ENABLED` | offer MusicBrainz matching on the Review page |
| `inbox_quiet_seconds` | `120` | `DC_INBOX_QUIET_SECONDS` | how long a dropped file must sit unchanged before it is filed |
| `navidrome_url` | | `DC_NAVIDROME_URL` | required to sign in |
| `navidrome_user` | | `DC_NAVIDROME_USER` | admin account, for triggering scans |
| `navidrome_password` | | `DC_NAVIDROME_PASSWORD` | stored in plain text in `config.toml` |
| `navidrome_db` | `/navidrome/navidrome.db` | `DC_NAVIDROME_DB` | mounted read-only |
| `music_dir` | `/music` | `DC_MUSIC_DIR` | fallback library root |
| `output_dir` | `/downloads` | `DC_OUTPUT_DIR` | where each person's inbox lives |

## Files that matter

`config/state.db` holds the small amount that exists nowhere else: which
album UUID each album key maps to, your listening history, and which duplicate
groups you decided to keep as they are. The album registry cannot be derived
from the files — a UUID is invented rather than observed — and Navidrome keeps
only a cumulative play count, so the history here is the only copy.
**Back it up.**

`config/beets/<username>-<library id>/library.db` is that person's beets
index. It is rebuildable from the files, but not quickly.

`config/cookies.txt` is optional. If YouTube starts demanding sign-in, export
your cookies in Netscape format and drop them here; yt-dlp will use them.

## Behaviour worth knowing

**Already-downloaded tracks are skipped silently.** Matching is on Spotify ID
*and* ISRC, so the same recording issued under a different ID still counts as
held. Re-queue an album you already have and it finishes in under a second.
Scoped to the library the download would go into, so two people do not
share one answer. Clicking the "already have" tag in Browse forgets a
track, so it can be fetched again if the file has since left the library.

**Wrong matches fail rather than download.** Candidates are scored on runtime
against Spotify's exact duration, title and artist similarity, and penalised
for version markers ("live", "sped up", "karaoke") the Spotify title lacks.
Title and artist are hard gates. Anything below the confidence floor lands in
the failed list, where you can retry it — a wrong file is far more expensive
to undo once beets has imported it than a missing one is to fetch again.

**Restarting loses the queue.** Job state is in memory by design. Re-paste
the link; what already reached the library is marked "in library" in Browse,
and re-queuing it downloads a second copy rather than refusing.

**Nothing is deleted.** Duplicate losers are moved to `duplicates-removed/`,
never unlinked. Recovering one is a manual job, so read a group before
resolving it.

## Documentation

- [docs/PLAN.md](docs/PLAN.md) — what this is for and where it is going
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — how the code works, and the
  gotchas that each cost real time
- [docs/HANDOFF.md](docs/HANDOFF.md) — picking the work up
