# Navidrome Companion

A companion to [Navidrome](https://www.navidrome.org/): the things it has no
answer for, in a browser, from a phone or a computer.

- **Download music** — search Spotify or paste a link (Spotify, YouTube,
  Bandcamp, SoundCloud and anything else yt-dlp handles). Tracks are matched,
  tagged, given a permanent identity and filed into your library within
  seconds.
- **Drop files in** — drag an album onto the page, or copy it into your inbox
  over the network.
- **Keep your listening history** — Navidrome keeps only a running total; this
  records every play, with statistics by month, hour, artist, album and
  listening session.
- **Maintain the library** — edit tags inline, rename and merge albums, match
  against MusicBrainz, fix covers, measure ReplayGain, combine loose singles
  into albums, and set aside duplicates without deleting anything.
- **Edit smart playlist rules**, which Navidrome otherwise only reads from a
  file.
- **Check its health** — a list of numbers that should be zero.

It runs as one container beside Navidrome, sharing the music volumes and
reading Navidrome's database. You sign in with your Navidrome account, and
Navidrome stays the authority on accounts, libraries, stars and ratings;
nothing here keeps a second copy of any of it. Every person gets their own
library, inbox and history.

```bash
curl -O https://raw.githubusercontent.com/awdimartino/navidrome-companion/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/awdimartino/navidrome-companion/main/.env.example
nano .env
docker compose up -d
```

Navidrome needs two settings first, and every library must be mounted at the
same path in both containers — read the setup guide before starting.

## Documentation

- [docs/SETUP.md](docs/SETUP.md) — installing from scratch: Navidrome's
  configuration, mounts, settings, beets, updates, backups and the Raspberry
  Pi
- [docs/FEATURES.md](docs/FEATURES.md) — every feature in depth, and how it
  works underneath
- [docs/PLAN.md](docs/PLAN.md) — what it is for and what comes next
- [docs/CODE_REVIEW.md](docs/CODE_REVIEW.md) — known defects, by severity

Formerly *Download Center*. MIT licensed.
