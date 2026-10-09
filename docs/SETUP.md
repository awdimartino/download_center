# Setting up Navidrome Companion

From nothing to a working install: Navidrome configured for it, the
container running beside Navidrome, the first sign-in, and keeping it
updated. For what each part of the app does once it is running, see
[FEATURES.md](FEATURES.md).

---

## 1. What you need

- **Navidrome 0.58 or later**, already running in Docker. 0.58 is where
  multiple libraries arrived; which libraries an account may see is read from
  its `user_library` table, and on anything older non-admins get no library
  at all.
- **Docker with Compose v2.** The image is published for `linux/amd64` and
  `linux/arm64`. On a Raspberry Pi, use a 64-bit OS (see
  [section 9](#9-raspberry-pi)).
- **A Spotify developer app** for search, metadata and cover suggestions.
  Create one at <https://developer.spotify.com/dashboard>; any redirect URI
  will do, because only the client-credentials flow is used. Without it,
  Browse search and Spotify links do not work; direct links (YouTube,
  Bandcamp, SoundCloud, …) still do.
- **A Navidrome administrator account** the app can use as a service
  account, for triggering scans and fetching cover art. Optional, but without
  it new music waits for Navidrome's own scan schedule.

---

## 2. Plan the paths

This is the part that matters most, and the part that fails silently when it
is wrong.

**Every library must be mounted at the same path in both containers.**
Navidrome stores where each library lives in its own database, and the
companion reads that path and uses it as-is — there is no mapping between
the two. If Navidrome has a library at `/music`, the companion must see the
same files at `/music`. A second library at `/kelly` needs `/kelly` in both.

If a library is missing from the companion's mounts, the app refuses to
write to it and names the path. (Before that check existed, the files were
written into the container's own writable layer, reported as filed, and
destroyed by the next `docker compose pull`.)

You need four host directories:

| Host directory | Mounted at | Holds |
|---|---|---|
| your library (one per Navidrome library) | the same path Navidrome uses, e.g. `/music` | the music |
| scratch space | `/downloads` | one inbox per person and library. Empty at rest |
| config | `/config` | `config.toml`, `state.db`, beets configs, `cookies.txt` |
| Navidrome's data directory | `/navidrome` (read-only) | `navidrome.db` and its WAL files |

Put scratch space on the **same disk** as the library if you can: a finished
download is then filed with a rename rather than a copy.

Mount Navidrome's **data directory**, not just `navidrome.db`. Navidrome
runs SQLite in WAL mode, and reading the database correctly needs the
`navidrome.db-wal` and `navidrome.db-shm` files that sit beside it.

---

## 3. Configure Navidrome

### Track and album identity

The companion stamps two tags on every file it files: `navidrome_uuid`
(this file) and `navidrome_album_uuid` (the record it belongs to). Navidrome
has to be told to use them as its persistent ids, which is what lets stars,
ratings and play counts survive re-tagging, moving, renaming and a rebuilt
database.

Add this to `navidrome.toml` in Navidrome's data directory:

```toml
Tags.navidrome_uuid.Aliases = ['NAVIDROME_UUID']
Tags.navidrome_album_uuid.Aliases = ['NAVIDROME_ALBUM_UUID']
Tags.navidrome_album_uuid.Album = true

PID.Track = 'navidrome_uuid|musicbrainz_trackid|albumid,discnumber,tracknumber,title'
PID.Album = 'navidrome_album_uuid|musicbrainz_albumid|albumartistid,album,albumversion,releasedate'
```

(The equivalent environment variables are `ND_PID_TRACK` and
`ND_PID_ALBUM`; the tag aliases are only settable in the file.)

**Never use a single-field chain** such as `PID.Track = 'navidrome_uuid'`.
Navidrome hashes the empty string when a tag is missing, so every file
without the tag would get the same id and collapse into one track. The
fallbacks after the `|` are what keep untagged files distinct.

Restart Navidrome and run a **full** scan afterwards. Changing the PID
settings changes the ids Navidrome computes, so do this on a fresh install,
or back up `navidrome.db` first on an existing one.

### An existing collection

Only files that pass through the companion are stamped. A library that was
there first keeps working — Navidrome falls back to the MusicBrainz id and
then the album/disc/track/title chain — but Health will report those files
under *Tracks with no UUID*, and their identity is only as stable as those
fallbacks. To bring a collection fully in, copy it into a person's inbox
(section 7): each file is stamped and filed under
`Album Artist/Album/NN - Title.ext`, which means **it is moved** into that
layout.

### Libraries and users

Create libraries and users in Navidrome as usual, and give each user access
to their library there. The companion has no user list of its own: a new
user signs in once with their Navidrome account and everything follows from
which libraries Navidrome says they can see.

---

## 4. Install the container

In the directory you keep your compose files in:

```bash
curl -O https://raw.githubusercontent.com/awdimartino/navidrome-companion/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/awdimartino/navidrome-companion/main/.env.example
nano .env
```

`.env` holds every host-specific value, so `docker-compose.yml` does not
need editing for a single library:

| Variable | Meaning |
|---|---|
| `NC_IMAGE` | the image to run; `ghcr.io/awdimartino/navidrome-companion:latest` |
| `LIBRARY_DIR` | the library, mounted at `/music` |
| `WORKSPACE_DIR` | scratch space, mounted at `/downloads` (`STAGING_DIR` before 2026-10-09) |
| `CONFIG_DIR` | config, mounted at `/config` |
| `NAVIDROME_DATA_DIR` | Navidrome's data directory, mounted read-only at `/navidrome` |
| `PUID` / `PGID` | the user that should own written files |
| `TZ` | container timezone |
| `PORT` | host port to serve on (default 8000) |
| `NC_NAVIDROME_URL` | where Navidrome answers, **from inside this container** |
| `NC_SPOTIFY_CLIENT_ID` / `_SECRET` | optional; can be set in the UI instead |

**`PUID`/`PGID`** must match the owner of your library directory, or files
written here will not be readable by Navidrome. Find it with
`stat -c '%u %g' /path/to/library`. The entrypoint remaps its user to these
ids, takes ownership of `/config`, and touches only the top directory of
`/downloads` and `/music` — never recursively, since a library may hold
files owned by others.

**`NC_NAVIDROME_URL`** has to be set before the first sign-in, because the
Settings panel that can also set it is behind sign-in. If Navidrome is a
service called `navidrome` on the same compose network, use
`http://navidrome:4533`. If it is published on the host, use the host's
address — `localhost` inside a container is the container itself.

### More than one library

Add a line per library under `volumes:` in `docker-compose.yml`, at the path
Navidrome uses:

```yaml
      - ${LIBRARY_DIR:-./library}:/music
      - /srv/music-kelly:/kelly
```

### Navidrome and the companion in one compose file

A sketch of the two services side by side, showing the paths that must
agree:

```yaml
services:
  navidrome:
    image: deluan/navidrome:latest
    user: 1000:1000
    ports: ["4533:4533"]
    volumes:
      - /srv/navidrome/data:/data
      - /srv/music:/music:ro

  navidrome-companion:
    image: ghcr.io/awdimartino/navidrome-companion:latest
    container_name: navidrome-companion
    restart: unless-stopped
    ports: ["8000:8000"]
    environment:
      PUID: 1000
      PGID: 1000
      NC_NAVIDROME_URL: http://navidrome:4533
    volumes:
      - /srv/navidrome-companion/config:/config
      - /srv/navidrome-companion/workspace:/downloads
      - /srv/music:/music                  # same path as Navidrome's
      - /srv/navidrome/data:/navidrome:ro  # the directory, read-only
```

Navidrome only needs read access to the library; the companion needs write
access.

### Start it

```bash
docker compose up -d navidrome-companion
docker compose logs -f navidrome-companion
docker inspect navidrome-companion --format '{{.State.Health.Status}}'
```

The container reports `healthy` once `/healthz` answers. `restart:
unless-stopped` brings it back after a reboot.

---

## 5. First sign-in and Settings

Open `http://<host>:8000` and sign in with a Navidrome account. Your
password goes to Navidrome and is never stored; the session is held in
memory for 14 days, so restarting the container signs everyone out.

Then, signed in as a **Navidrome administrator**, open **Settings** from the
menu:

- **Navidrome service account** (`navidrome_user`, `navidrome_password`):
  an admin account used to start a scan when a download finishes, after a
  Library edit, and to fetch cover art. (Music dropped into an inbox or
  uploaded on the Drop page does not trigger a scan yet; it appears on
  Navidrome's own schedule, or after **Rescan** in Library.) Navidrome only lets admins start scans. The password is
  stored in plain text in `config.toml`.
- **Spotify** client id and secret.
- **Downloads**: simultaneous downloads (shared across the whole server),
  MP3 bitrate, attempts per track, and the pause between downloads.

Only administrators can see or change Settings, because they hold
credentials and decide where every library's music goes. Secrets are never
sent back to the browser; a blank secret field means "leave it as it is".

### Every setting

Settings live in `/config/config.toml`. Each key can also be set with an
environment variable, `NC_` plus the key in capitals, and **the environment
wins** over the file. (The prefix was `DC_` before 2026-10-09; those names
are still read, with a warning in the log naming each one to rename.) A key set by the environment is shown locked in the
panel, since an edit there would revert at the next restart, and its value
is never written to `config.toml`. (Before October 2026 saving the panel did
copy environment secrets into the file; check yours if you set
`DC_NAVIDROME_PASSWORD` or `DC_SPOTIFY_CLIENT_SECRET` (as they were then named).)

| Key | Default | In the panel | Meaning |
|---|---|---|---|
| `navidrome_url` | | yes | where Navidrome answers. Required to sign in |
| `navidrome_user` | | yes | service account for scans and cover art |
| `navidrome_password` | | yes | its password, plain text |
| `navidrome_db` | `/navidrome/navidrome.db` | no | Navidrome's database, opened read-only |
| `spotify_client_id` | | yes | Spotify app |
| `spotify_client_secret` | | yes | Spotify app |
| `concurrency` | `3` | yes | simultaneous downloads, whole server (1–10) |
| `audio_bitrate` | `320` | yes | MP3 kbps, 32 to 320, or a VBR level 0 to 9; a source at or below 192 kbps is encoded at 192 |
| `max_attempts` | `3` | yes | tries per track before it fails (1–10) |
| `rate_limit_sleep` | `2.0` | yes | seconds between downloads, up to 300 |
| `inbox_quiet_seconds` | `120` | no | how long a dropped file must sit unchanged before it is filed |
| `beets_enabled` | `true` | yes (checkbox) | offer MusicBrainz matching in Library |
| `play_day_timezone` | `UTC` | no | which midnight ends a listening day, e.g. `America/New_York` |
| `music_dir` | `/music` | no | fallback library root; where free space is measured |
| `output_dir` | `/downloads` | no | where each person's inbox lives |
| `acoustid_key` | | yes | for `tools/fingerprint.py` only |
| `lastfm_api_key`, `lastfm_secret` | | no | the Last.fm import, and the Download tab's similar artists, songs and genre picks (only the API key is needed for those) |

Set `play_day_timezone` to where you listen. Days, months and the hour of
day in the listening statistics are cut in that zone; the default UTC puts
an evening's listening on the next day for anyone in the Americas.

---

## 6. beets

beets is used for one thing: **Find matches** in the Library, which asks
MusicBrainz what an album is and retags it as the release you pick. It
never files or moves anything.

Nothing needs setting up. The first time a person uses it, a config is
written to `/config/beets/<username>-<library id>/config.yaml`, one per
person and library, since its `directory` is that library's root. Applying a
match uses a throwaway beets index for that one run, so the `library.db`
older installs have beside the config is no longer read. The file is
**never overwritten**, so edit it freely — and
note that an older install keeps whatever template it was first given.
Configs written before September 2026 say `move: yes`. Applying a match
overrides `move`, `copy` and `write` whatever the file says, so that is
harmless, but changing it to `move: no` keeps the file honest.

The settings in it that matter:

- `import: move: no, copy: no, write: yes` — beets only writes tags. Where a
  file lives is decided by the companion alone.
- `plugins: musicbrainz fetchart embedart chroma` — `musicbrainz` must be
  named. Since beets 2.x it is a plugin, and naming any plugins replaces
  the default list, so leaving it out disables album matching entirely
  ("Evaluating 0 candidates").
- `musicbrainz: data_source_mismatch_penalty: 0` — with chroma loaded,
  beets otherwise charges every MusicBrainz candidate a 0.5 penalty.
- `ui: color: no` — colour codes corrupt the output the app parses.
- `quiet_fallback: skip` — beets never guesses; you choose.

`fpcalc` (Chromaprint) is in the image, which the `chroma` plugin uses to
identify audio by fingerprint as well as by tags.

To turn matching off entirely, untick *Find matches* in Settings (or set
`beets_enabled = false`).

---

## 7. Optional extras

**YouTube cookies.** If YouTube starts refusing downloads (403s, "sign in to
confirm you're not a bot"), export your browser's YouTube cookies in
Netscape format to `/config/cookies.txt`. yt-dlp picks the file up on the
next download; no restart needed.

**The inbox on a network share.** Each person has an inbox at
`<scratch>/<username>-<library id>/inbox/`, created the first time they
download or upload something. Anything copied in there is filed into their library once it has sat
unchanged for `inbox_quiet_seconds` — so sharing that folder over SMB gives
a drag-and-drop route in from any computer, beside the Drop page in the app.
The `.owner` file in each workspace says whose it is; do not delete it.

**HTTPS.** The app serves plain HTTP and sign-in posts a Navidrome password.
On anything other than a trusted network, put it behind a reverse proxy
with TLS. The session cookie is marked `secure` when the request arrives
over HTTPS. FastAPI's `/docs`, `/redoc` and `/openapi.json` need a
signed-in session, like the API they describe.

Anything that changes something (a POST, PUT or DELETE) and the live
socket are refused unless the browser says they came from this app's own
page. Modern browsers say so with `Sec-Fetch-Site`; for one that does not,
the `Origin` is compared with the `Host` asked for, so a reverse proxy
should pass the original `Host` (or `X-Forwarded-Host`) through.

---

## 8. Updating, backups and maintenance

**Updating** is a pull and a restart:

```bash
docker compose pull navidrome-companion
docker compose up -d navidrome-companion
```

Every push to `main` builds and publishes a new image after the tests pass.
Version tags (`v1.2.3`) also publish `1.2.3` and `1.2` tags if you would
rather pin. Restarting signs everyone out and forgets the download queue —
anything already filed is safe.

**Back up `/config/state.db`.** It holds what exists nowhere else: the album
identity registry (an album UUID is invented, not observed, so it cannot be
rebuilt from the files), your listening history (Navidrome keeps only a
running total), and the record of everything quarantined. Copy it while the
container is stopped, or with `sqlite3 state.db ".backup state.db.bak"`.

`/config/beets/*/library.db` is no longer used by the app and need not be
backed up. (`python -m app.reindex` still rebuilds it, for using beets by
hand.)

**Maintenance commands** run inside the container. Run them as the app's
user so new files get the right owner — `docker exec` is root by default,
and each command refuses to run as root unless `NC_ALLOW_ROOT=1` is set:

```bash
docker exec -u companion navidrome-companion python -m app.survey
```

| Command | What it does |
|---|---|
| `python -m app.survey [--json]` | read-only report of every library's album identity: ready, partial, unstamped, split, fused |
| `python -m app.backfill [--apply]` | records album UUIDs for albums whose files already agree. Writes only `state.db` |
| `python -m app.unfuse [--apply] [--plan FILE]` | repairs albums sharing one UUID or split across several, writing only the album UUID tag. `--plan FILE` saves a reversible plan per library, as `FILE-<library id>` |
| `python -m app.relink [--apply] [--undo]` | credits a song's listening history, recorded under a track UUID Navidrome can no longer find, to the live copy of the same song (same library, artist, title and length). Writes only aliases in `state.db`; `--undo` removes them |
| `python -m app.separate [--user NAME] [--apply] [--plan FILE]` | moves the tracks of a second album out of a folder that holds two, to the folder their tags name. The album the folder is named for stays; files with no album tag never move. `--plan FILE` records where each file went, per library |
| `python -m app.reindex <user> [--library ID] [--apply]` | rebuilds a person's beets index from the files in place |
| `python -m app.lastfm <user> [--apply] [--times]` | one-off import of Last.fm scrobbles into the listening history; `--times` recovers exact play times for rows already imported |

Each is a dry run until `--apply`.

`tools/fingerprint.py` (AcoustID lookups for files with no MusicBrainz id)
and `tools/fix_broken_m4a.py` (repairs `.m4a` files that are not real MP4
containers) are in the image too, run as `python -m tools.<name>`; each
script's own usage says how. Run with `docker run --entrypoint`, pass
`--user 1000:1000`, since that skips the image's drop from root.

---

## 9. Raspberry Pi

**Use a 64-bit OS.** Check with `uname -m`: `aarch64` is right. On 32-bit
`armv7l` several dependencies have no prebuilt wheels and must compile on
the Pi, which is slow and can run out of memory. Reinstall with Raspberry Pi
OS (64-bit) rather than fight it.

There is no build on the Pi and no git checkout: it pulls the published
arm64 image. It needs only `docker-compose.yml`, `.env` and the config
directory.

Things that have bitten this deployment:

- **Wi-Fi.** A Pi on a weak Wi-Fi link can drop UDP badly enough that DNS
  fails inside containers while the host seems fine — beets then cannot
  reach MusicBrainz. Wired ethernet fixes it. Turn Wi-Fi power saving off
  if wired is not an option.
- **Service names in URLs.** If you give the container explicit `dns:`
  servers, Docker's embedded DNS stops resolving LAN host names, and a
  `navidrome_url` like `http://alex-pi:4533` fails. Use the compose service
  name, or add `extra_hosts: ["alex-pi:host-gateway"]`.
- **Memory limits.** On Raspberry Pi OS the memory cgroup controller is off
  by default, so compose `mem_limit` settings are silently ignored.

### Moving from the old name

The project was called Download Center. An install that predates the
rename keeps working, but stops receiving updates: the image is now
published as `ghcr.io/awdimartino/navidrome-companion`. To move over, in
the compose file rename the service and container to `navidrome-companion`
and change `image:`. Then:

```bash
docker compose stop download-center && docker compose rm -f download-center
docker compose up -d navidrome-companion
```

Keep the same volumes; config and `state.db` carry over unchanged.

The second half of the rename, on 2026-10-09, changed what was left:

- **Environment variables** are `NC_…` instead of `DC_…`, and the
  scratch-space variable is `WORKSPACE_DIR` instead of `STAGING_DIR`. The
  old names still work, for the app and in the shipped compose file, and
  each `DC_` one is named in the log at start-up until it is renamed.
- **The container user** is `companion` instead of `downloader`, with the
  same uid, so files on disk keep their owner. Scripts that run
  `docker exec -u downloader` need the new name.
- **The session cookie** is `nc_session`, so everyone is signed out once.
- **The default workspace** when running from source is `workspace/`
  instead of `untagged/`.

Moving the host's config folder is optional and up to you: stop the
container, back up `state.db`, move the folder, and point the `/config`
mount at its new place.

---

## 10. Running from source

For development, or to build your own image:

```bash
git clone https://github.com/awdimartino/navidrome-companion.git
cd navidrome-companion
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt     # .venv\Scripts on Windows
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
```

To build the image instead of pulling it, swap `image:` for `build: .` in
`docker-compose.yml`. CI (`.github/workflows/docker.yml`) compiles every
module, runs ruff and the test suite, and only then builds amd64 and arm64
and pushes to GHCR. Pull requests get the tests without a publish.

There is no JavaScript build step: the front end is ES modules under
`app/static/js/`, served as they are. `tests/test_frontend.py` checks that
the markup, scripts and stylesheet still agree with each other; nothing
executes the JavaScript, so check UI changes in a browser.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| "Navidrome is not configured" at sign-in | `NC_NAVIDROME_URL` is not reaching the container. It must be in compose's `environment:`, not only in `.env` |
| "Could not reach Navidrome" | the URL is wrong from inside the container (`localhost` is the container itself) |
| Signed in, but "no library assigned" | the `/navidrome` mount is missing, or the user has no library in Navidrome |
| A refusal naming a library path | that library is not mounted here at the path Navidrome uses |
| New music takes minutes to appear | no service account in Settings, so it waits for Navidrome's scheduled scan. Drops and uploads always wait; press Rescan in Library |
| Downloads fail with 403 | YouTube wants cookies; see `cookies.txt` above |
| Find matches finds nothing, ever | no DNS from the container, or `musicbrainz` missing from that person's beets `plugins:` |
| Files written but Navidrome cannot read them | `PUID`/`PGID` do not match the library's owner |
| Home says play counts have not been read | Navidrome's database cannot be opened; check the `/navidrome` mount |
| A change seems missing after an update | the browser holds an old script. The shell stamps assets with a version, so a normal reload fixes it; check `docker compose pull` actually fetched a new image |
