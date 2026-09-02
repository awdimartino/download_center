# Download Center

Takes a Spotify link, finds the matching recording on YouTube Music, downloads
it as a 320 kbps MP3, writes Spotify's metadata onto it, and stages it for
beets to import.

beets is bundled and runs automatically when a job finishes, so the pipeline
is self-contained:

```
paste a link -> download -> seed tags -> beets -> tagged library -> Navidrome
```

The tags this writes are deliberately provisional. They exist so beets has
something accurate to match against; beets then decides the canonical release,
writes the MusicBrainz identifiers, fetches cover art, and files the result
into the tree defined in its own config. Matching a whole album at once, on
track count, ordering, durations and artist together, is far more reliable
than any per-track identifier lookup.

## What it handles

**Spotify** track, album, and playlist links. Metadata comes from the Spotify
API and the audio is located on YouTube Music by matching on runtime, title
and artist. Artist links are rejected deliberately, since a discography is
rarely what you meant to queue.

**Direct links** to anything yt-dlp supports - YouTube, YouTube Music,
SoundCloud, Bandcamp and hundreds of others - including playlists. These skip
the matching stage entirely, because the URL already names the audio.

Tag quality on direct links depends on what the site publishes. YouTube Music,
Bandcamp and `- Topic` channels expose real track, artist and album fields and
tag cleanly. A plain upload offers only a video title, so artist and title are
guessed by splitting on " - ", which follows the usual convention but will get
it backwards on a title like "Game Over - Free Metal Instrumental". Direct
links are always staged as singles, since there is no track count to prove a
release is complete.

## Tagging and filing

On first run a beets config is written to `config/beets/config.yaml` and never
overwritten afterwards. Edit it to change where files land:

```yaml
directory: /music
paths:
  default: $albumartist/$album%aunique{} ($original_year)/$track $title
  singleton: Singles/$artist - $title
```

Imports run unattended with `quiet_fallback: skip`, so anything beets cannot
confidently match is **left in the staging folder** rather than guessed at.
A staging directory that is not empty is the list of things needing a look.

`fpcalc` is in the image, so acoustic fingerprinting can be enabled by adding
`chroma` to the plugins line - useful if you want identification by audio
rather than by tags.

Set `beets_enabled = false` to turn all of this off and keep the staging
output as the final result.

## Library panel

The Library tab reads the beets database directly and shows what is actually
in the library, searchable with beets' own query syntax:

```
radiohead              artist or album text
year:1997              a field
albumartist:burial     a specific field
year:1990..1999        a range
```

Above it sits **Needs attention**: everything still in the staging folder,
which by definition is what beets declined to identify. Pressing *Identify*
asks beets' matcher for candidate releases and lists them with a confidence
score, country, label, media and track count, so the different pressings of a
record are distinguishable. Choosing one imports under that release; *Import
as-is* keeps the existing tags and files it without matching; *Discard*
deletes it.

Picking a release deliberately raises beets' strong-match threshold for that
one import. Without that, a 94% match is still refused in quiet mode, and
naming the release would appear to do nothing.

## Browsing

The Browse tab searches Spotify from inside the app, so you never need to go
and copy a link. Search albums, tracks, or artists; open an album to see its
track listing, or an artist to see their discography with duplicate reissues
collapsed. Tracks already in the ledger are marked, so you can tell at a
glance what you are missing from a record before queuing it.

## Staging layout

Output is split by how beets should import it:

```
untagged/
  albums/           complete releases      ->  beet import
    Radiohead - OK Computer/
      01 - Airbag.mp3
      ...
  singles/          loose tracks           ->  beet import -s
    Burial - Archangel.mp3
```

An album only lands in `albums/` when **every** one of its tracks was
downloaded. Playlist tracks, single-track jobs, and albums where something
failed all go to `singles/` instead, because beets cannot album-match a
fragment of a release and will skip it under `--quiet`.

Files are built in `untagged/.incomplete/` and moved into place only once a
job finishes, so a beets cron firing mid-download never sees a partial album.

## Setup

```bash
curl -O https://raw.githubusercontent.com/awdimartino/download_center/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/awdimartino/download_center/main/.env.example
nano .env                            # set DC_IMAGE, paths, PUID/PGID
docker compose up -d
```

Then open <http://localhost:8000>. Enter your Spotify credentials in the
Settings panel, or set them in `.env` beforehand.

Images are published to GitHub Container Registry for `linux/amd64` and
`linux/arm64`, so the same tag works on a PC or a Raspberry Pi:

```bash
docker pull ghcr.io/awdimartino/download_center:latest
```

To build from source instead, clone the repo and swap `image:` for `build: .`
in `docker-compose.yml`.

Get credentials at <https://developer.spotify.com/dashboard>. Any redirect URI
will do; this only uses the client-credentials flow.

All host paths come from `.env`, so `docker-compose.yml` never needs editing:

| Variable | Meaning |
|---|---|
| `MUSIC_DIR` | where finished audio is written |
| `CONFIG_DIR` | where `config.toml` and `state.db` live |
| `PUID` / `PGID` | user that should own the written files |
| `PORT` | host port to serve on |

## Deploying to a Raspberry Pi

**Use a 64-bit OS.** Check first:

```bash
uname -m        # aarch64 = good;  armv7l = see below
```

On `aarch64` every dependency has a prebuilt wheel and the image builds in a
couple of minutes. On 32-bit `armv7l`, rapidfuzz, uvloop and httptools have no
wheels and must compile from C++ source on the Pi, which is slow and can
exhaust memory. Reinstall with Raspberry Pi OS (64-bit) rather than fight it.

Find the user that owns your music directory, so the container writes files
that the rest of your setup can actually read:

```bash
stat -c '%u %g' /mnt/music        # prints e.g. "1000 1000"
```

Copy the project across, excluding local build artefacts:

```bash
rsync -av --exclude .venv --exclude untagged --exclude config       ./ pi@raspberrypi.local:~/download-center/
```

Then on the Pi:

```bash
cd ~/download-center
cp .env.example .env
nano .env                          # set MUSIC_DIR, CONFIG_DIR, PUID, PGID
docker compose up -d --build
```

The build runs natively on the Pi, so there is no cross-compilation or
registry to set up. `restart: unless-stopped` brings it back after a reboot.

Check it came up:

```bash
docker compose logs -f
docker inspect download-center --format '{{.State.Health.Status}}'
```

## Configuration

Set in `config/config.toml`, or from the Settings panel in the UI. Every key
can be overridden by an environment variable, which wins over the file.

| Key | Default | |
|---|---|---|
| `spotify_client_id` | | `DC_SPOTIFY_CLIENT_ID` |
| `spotify_client_secret` | | `DC_SPOTIFY_CLIENT_SECRET` |
| `concurrency` | `3` | simultaneous downloads |
| `audio_bitrate` | `320` | MP3 kbps |
| `max_attempts` | `3` | tries per track before failing |
| `rate_limit_sleep` | `2.0` | seconds between downloads |

## Files that matter

`config/state.db` is the download ledger. Because beets *moves* files out of
the staging folder, the filesystem cannot answer "do I already have this
track" - this database is the only thing that can. **Back it up.** Losing it
means re-downloading everything you queue again.

`config/cookies.txt` is optional. If YouTube starts demanding sign-in, export
your cookies in Netscape format and drop them here; yt-dlp will use them.

## Behaviour worth knowing

**Duplicates are skipped silently.** Matching is on Spotify ID *and* ISRC, so
the same recording issued under a different ID still counts as held. Re-queue
an album you already have and it finishes in under a second.

**Wrong matches fail rather than download.** Candidates are scored on runtime
against Spotify's exact duration, title and artist similarity, and penalised
for version markers ("live", "sped up", "karaoke") the Spotify title lacks.
Title and artist are hard gates. Anything below the confidence floor lands in
the failed list, where you can retry it - a wrong file is far more expensive
to undo once beets has imported it than a missing one is to fetch again.

**Restarting loses the queue.** Job state is in memory by design; only the
ledger is persisted. Re-paste the link and everything already downloaded is
skipped.
