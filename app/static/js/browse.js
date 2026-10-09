"use strict";

/* --- browse ----------------------------------------------------------------
   One box: a link queues a download, anything else searches Spotify. The
   results come straight under it and the queue lives beside them
   (downloads.js), so finished downloads never stand between a search and
   its answer.

   Every result says for itself where it stands - in the library, part of
   it in the library, queued, downloading, failed - painted from the same
   jobs the Downloads panel draws, so nobody has to go and look. */

import { apiFetch, duration, el, libraryPicker, pageEscape, plural, remoteArt, showError, songRow, targetLibrary } from "./core.js";
import { jobs, onJobs } from "./ws.js";
import { MOVING, expectJob, jobGroup, jobProgress, settledCount } from "./downloads.js";

const form = document.getElementById("search-form");
const input = document.getElementById("query");
const goEl = document.getElementById("search-go");
const hintEl = document.getElementById("link-hint");
const kindsEl = document.getElementById("browse-kinds");
const resultsEl = document.getElementById("results");
const emptyEl = document.getElementById("browse-empty");
const drawerEl = document.getElementById("browse-drawer");
const forYouEl = document.getElementById("for-you");

const KINDS = ["all", "album", "track", "artist"];
let kind = "all";
try {
  const saved = localStorage.getItem("browse.kind");
  if (KINDS.includes(saved)) kind = saved;
} catch { /* a private window starts on All */ }

// What the server queues rather than searches: any http(s) link
// (generic.looks_like_url), and a Spotify link in the forms people paste
// without the scheme (spotify.is_spotify).
function looksLikeUrl(text) {
  return /^(?:https?:\/\/|spotify:(?:track|album|playlist|artist):|(?:open\.spotify\.com|spotify\.link)\/)/i
    .test(text.trim());
}


/* --- where a result stands ------------------------------------------------
   Read from the jobs on every change. Each node on screen registers how to
   repaint itself, and a job message repaints only those - the results are
   never rebuilt because a progress bar moved. */

let painters = [];          // repaint callbacks for the results on screen
let drawerPainters = [];    // and for the open album
// And for the recommendation shelves (foryou.js), which outlive any one
// search. Emptied in place by whoever rebuilds the shelves.
export const shelfPainters = [];
const asked = new Set();    // links pressed whose job has not arrived yet

function repaint() {
  for (const url of asked) {
    if ([...jobs.values()].some((j) => j.source_url === url)) asked.delete(url);
  }
  painters.forEach((paint) => paint());
  drawerPainters.forEach((paint) => paint());
  shelfPainters.forEach((paint) => paint());
}
onJobs(repaint);

function painted(list, node, paint) {
  list.push(paint);
  paint();
  return node;
}

// The Spotify id a job was queued from, and what kind of thing it was.
function sourceOf(job) {
  const m = /spotify\.com\/(?:intl-[\w-]+\/)?(album|track|playlist)\/([A-Za-z0-9]+)/
    .exec(job.source_url || "");
  return m ? { kind: m[1], id: m[2] } : null;
}

function newestFirst() {
  return [...jobs.values()].sort((a, b) => b.created_at.localeCompare(a.created_at));
}

function albumJob(album) {
  return newestFirst().find((job) => {
    const source = sourceOf(job);
    return source && source.kind === "album" && source.id === album.id;
  });
}

// "pending", a moving step, "complete", "failed", "held" or null.
function trackState(track) {
  if (asked.has(track.url)) return "pending";
  for (const job of newestFirst()) {
    const source = sourceOf(job);
    if (job.status === "resolving" && source && source.kind === "track" && source.id === track.id) {
      return "pending";
    }
    const item = job.items.find((i) => i.spotify_id === track.id);
    if (item && item.status !== "cancelled") return item.status;
  }
  return track.held ? "held" : null;
}

const STATE = {
  held: ["In library", "tone-ok"],
  complete: ["In library", "tone-ok"],
  pending: ["Queued", ""],
  matching: ["Finding", "tone-accent"],
  downloading: ["Downloading", "tone-accent"],
  retrying: ["Retrying", "tone-accent"],
  tagging: ["Tagging", "tone-accent"],
  filing: ["Filing", "tone-accent"],
  failed: ["Failed", "tone-bad"],
};

function stateBadge(state) {
  const known = STATE[state];
  return known ? el("span", `lib-pill ${known[1]}`.trim(), known[0]) : el("span");
}

// The download arrow, drawn by the stylesheet (.br-arrow) in currentColor.
function arrow() {
  const icon = el("span", "br-arrow");
  icon.setAttribute("aria-hidden", "true");
  return icon;
}

function withArrow(button, label) {
  button.replaceChildren(arrow(), el("span", "br-get-label", label));
  return button;
}

/* --- queueing ------------------------------------------------------------- */

async function queue(url, title, cover) {
  if (!url) return;
  showError("");
  expectJob(url, title, cover);
  asked.add(url);
  repaint();
  try {
    const response = await apiFetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, library_id: targetLibrary() }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      showError(body.detail || `Could not queue that (${response.status}).`);
      asked.delete(url);
      repaint();
      return false;
    }
    return true;
  } catch {
    showError("Could not reach the server.");
    asked.delete(url);
    repaint();
    return false;
  }
}

// The button on a song. Already owned is still allowed - it arrives as a
// second copy - but it is quiet, so it is not the obvious thing to press.
function trackButton(track) {
  const button = el("button", "ghost br-get");
  button.type = "button";
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    queue(track.url, `${track.artist} - ${track.name}`, track.cover);
  });
  const paint = () => {
    const state = trackState(track);
    const busy = state === "pending" || MOVING.has(state);
    const owned = state === "held" || state === "complete";
    const label = busy ? "Queued" : state === "failed" ? "Try again" : owned ? "Again" : "Download";
    withArrow(button, label);
    button.disabled = busy;
    button.classList.toggle("quiet", owned);
    button.title = owned
      ? "Already in your library. Downloading again makes a second copy."
      : label;
    button.setAttribute("aria-label", `${label}: ${track.name}`);
  };
  return { button, paint };
}

/* --- albums ---------------------------------------------------------------- */

function releaseKind(album) {
  if (album.type === "compilation") return "Compilation";
  if (album.type === "single") return album.total > 1 ? "EP" : "Single";
  return null;
}

// The state on a cover: a veil and a bar while downloading, otherwise a
// flag saying how much of it you have, and the download button.
function paintCover(album, overlay) {
  const job = albumJob(album);
  const group = job && jobGroup(job);
  const parts = [];
  const total = album.total || 0;
  const held = album.held_tracks || 0;

  // Rebuilt only when something it shows has changed. Every progress tick
  // repaints every cover, and replacing a button under the pointer drops
  // its hover and its focus.
  const sig = [asked.has(album.url), group, job && settledCount(job), job && job.items.length,
               job && Math.round(jobProgress(job) * 100), job && job.items.some((i) => i.status !== "pending")].join("|");
  if (overlay.dataset.sig === sig) return;
  overlay.dataset.sig = sig;

  if (asked.has(album.url) || group === "active") {
    const veil = el("div", "br-veil");
    const words = el("div", "br-veil-text");
    const started = job && job.items.some((i) => i.status !== "pending");
    words.append(started ? "Downloading" : "Queued",
                 el("small", "", job && job.items.length
                   ? `${settledCount(job)} of ${job.items.length} tracks`
                   : "Reading the album…"));
    const bar = el("div", "br-veil-bar");
    const fill = el("div", "br-veil-fill");
    fill.style.width = `${Math.round((job ? jobProgress(job) : 0) * 100)}%`;
    bar.append(fill);
    veil.append(words, bar);
    overlay.replaceChildren(veil);
    return;
  }

  const flags = el("span", "lib-flags");
  if (group === "done" || (total && held >= total)) {
    flags.append(el("span", "lib-flag tone-ok", "In library"));
  } else {
    if (group === "attention") flags.append(el("span", "lib-flag tone-bad", "Some failed"));
    else if (held) flags.append(el("span", "lib-flag", `${held} of ${total} in library`));
    const quick = el("button", "br-quick");
    quick.type = "button";
    quick.append(arrow());
    quick.title = held
      ? `Download ${album.name}. The ${held} you have would arrive as second copies.`
      : `Download ${album.name}`;
    quick.setAttribute("aria-label", `Download ${album.name}`);
    quick.addEventListener("click", (event) => {
      event.stopPropagation();
      queue(album.url, `${album.artist} - ${album.name}`, album.cover);
    });
    parts.push(quick);
  }
  overlay.replaceChildren(flags, ...parts);
}

function albumCard(album) {
  return buildAlbumCard(album, painters);
}

// A cover on a shelf: the same card, repainted with the shelves, and with
// its line under the title chosen by the shelf ("released 12 Sep").
export function shelfAlbum(album, sub) {
  return buildAlbumCard(album, shelfPainters, sub);
}

function buildAlbumCard(album, list, sub) {
  const card = el("div", "lib-card br-card");
  card.tabIndex = 0;
  card.setAttribute("role", "button");
  card.title = `${album.artist} - ${album.name}`;
  const cover = el("div", "lib-card-cover");
  const overlay = el("div", "br-overlay");
  cover.append(remoteArt(album.cover), overlay);
  card.append(
    cover,
    el("span", "lib-card-title", album.name),
    el("span", "lib-card-sub",
       sub || [album.artist, album.year, releaseKind(album)].filter(Boolean).join(" · ")));
  card.addEventListener("click", () => openAlbum(album.id));
  card.addEventListener("keydown", (event) => {
    if (event.target !== card || (event.key !== "Enter" && event.key !== " ")) return;
    event.preventDefault();
    openAlbum(album.id);
  });
  return painted(list, card, () => paintCover(album, overlay));
}

/* --- songs ----------------------------------------------------------------- */

function trackRow(track) {
  return buildTrackRow(track, painters);
}

export function shelfTrack(track) {
  return buildTrackRow(track, shelfPainters);
}

function buildTrackRow(track, list) {
  const albumLink = el("button", "br-link", track.album || "");
  albumLink.type = "button";
  albumLink.addEventListener("click", () => openAlbum(track.album_id));
  albumLink.disabled = !track.album_id;

  const state = el("span", "br-state");
  const { button, paint } = trackButton(track);
  const row = songRow("div", remoteArt(track.cover), track.name,
                      [track.artist, " · ", albumLink],
                      [state, el("span", "br-dur", duration(track.duration_ms)), button]);
  row.classList.add("br-song");
  return painted(list, row, () => {
    state.replaceChildren(stateBadge(trackState(track)));
    paint();
  });
}

/* --- artists --------------------------------------------------------------- */

function followers(n) {
  if (n == null) return "";
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M followers`;
  if (n >= 1e3) return `${Math.round(n / 1e3)}K followers`;
  return `${n} followers`;
}

function artistTile(artist) {
  return shelfArtist(artist, followers(artist.followers));
}

export function shelfArtist(artist, sub) {
  const tile = el("button", "lib-artist");
  tile.type = "button";
  const mosaic = el("div", "lib-mosaic");
  mosaic.append(remoteArt(artist.cover));
  tile.append(mosaic, el("span", "lib-card-title", artist.name),
              el("span", "lib-card-sub", sub));
  tile.addEventListener("click", () => openArtist(artist.id));
  return tile;
}

/* --- the results ------------------------------------------------------------ */

let searchToken = 0;
let lastQuery = "";
let lastResults = null;     // what the last search returned, for Back

function setMessage(text) {
  emptyEl.textContent = text || "";
  emptyEl.hidden = !text;
  syncForYou();
}

// The recommendations (foryou.js) fill the page while there is nothing
// else on it: no search typed, no results, no artist, no message. Watched
// rather than called from every place the results change, of which there
// are many and will be more.
function syncForYou() {
  forYouEl.hidden = Boolean(input.value.trim()) || resultsEl.childElementCount > 0
    || !emptyEl.hidden;
}
new MutationObserver(syncForYou).observe(resultsEl, { childList: true });

function section(title, count, body, more) {
  const head = el("h3", "lib-section-title", title);
  if (count != null) head.append(el("small", "", String(count)));
  if (more) {
    const all = el("button", "br-see", "See all");
    all.type = "button";
    all.addEventListener("click", () => setKind(more));
    head.append(all);
  }
  const box = el("section", "br-section");
  box.append(head, body);
  return box;
}

function grid(nodes, className = "lib-grid") {
  const box = el("div", className);
  box.append(...nodes);
  return box;
}

function syncKinds() {
  kindsEl.querySelectorAll("button").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.kind === kind)));
}

function showResults(data) {
  painters = [];
  const parts = [];
  if (data.type === "all") {
    if (data.artists.length) {
      parts.push(section("Artists", null, grid(data.artists.slice(0, 6).map(artistTile), "lib-artists br-artists"), "artist"));
    }
    if (data.tracks.length) {
      parts.push(section("Songs", null, grid(data.tracks.slice(0, 5).map(trackRow), "br-songs"), "track"));
    }
    if (data.albums.length) {
      parts.push(section("Albums", null, grid(data.albums.map(albumCard)), "album"));
    }
  } else if (data.results.length) {
    const render = { album: albumCard, track: trackRow, artist: artistTile }[data.type];
    const box = { album: "lib-grid", track: "br-songs", artist: "lib-artists br-artists" }[data.type];
    const title = { album: "Albums", track: "Songs", artist: "Artists" }[data.type];
    parts.push(section(title, data.results.length, grid(data.results.map(render), box)));
  }
  resultsEl.replaceChildren(...parts);
  setMessage(parts.length ? "" : `Nothing on Spotify matches “${lastQuery}”. Try fewer words, or paste a link.`);
}

async function runSearch() {
  const q = input.value.trim();
  if (!q || looksLikeUrl(q)) return;
  // Guards against a slow earlier request landing after a newer one.
  const token = ++searchToken;
  lastQuery = q;
  if (!resultsEl.childElementCount) setMessage("Searching…");
  resultsEl.classList.add("br-loading");
  try {
    const limit = kind === "all" ? 10 : 24;
    const response = await apiFetch(
      `/api/search?q=${encodeURIComponent(q)}&type=${kind}&limit=${limit}`);
    const data = await response.json();
    if (token !== searchToken) return;
    if (!response.ok) {
      painters = [];
      resultsEl.replaceChildren();
      setMessage(data.detail || "Search failed.");
      return;
    }
    lastResults = data;
    showResults(data);
  } catch {
    if (token === searchToken) setMessage("Search failed. Is the server reachable?");
  } finally {
    if (token === searchToken) resultsEl.classList.remove("br-loading");
  }
}

function setKind(next) {
  kind = next;
  try { localStorage.setItem("browse.kind", kind); } catch { /* fine */ }
  syncKinds();
  window.scrollTo({ top: 0 });
  runSearch();
}

/* --- one artist ------------------------------------------------------------- */

async function openArtist(id) {
  if (!id) return;
  closeAlbum();
  const token = ++searchToken;
  painters = [];
  resultsEl.replaceChildren();
  setMessage("Loading…");
  kindsEl.hidden = true;
  window.scrollTo({ top: 0 });
  try {
    const response = await apiFetch(`/api/artists/${encodeURIComponent(id)}/albums`);
    const data = await response.json();
    if (token !== searchToken) return;
    if (!response.ok) return setMessage(data.detail || "Could not load that artist.");

    const back = el("button", "ghost lib-back", lastResults ? `‹ Results for “${lastQuery}”` : "‹ Back");
    back.type = "button";
    back.addEventListener("click", backToResults);

    const head = el("div", "lib-artist-head");
    const mosaic = el("div", "lib-mosaic");
    mosaic.append(remoteArt(data.artist.cover));
    const words = el("div");
    words.append(el("h2", "lib-artist-name", data.artist.name),
                 el("span", "lib-card-sub",
                    [followers(data.artist.followers), plural(data.albums.length, "release")]
                      .filter(Boolean).join(" · ")));
    head.append(mosaic, words);

    const albums = data.albums.filter((a) => a.type === "album");
    const rest = data.albums.filter((a) => a.type !== "album");
    const parts = [back, head];
    if (albums.length) parts.push(section("Albums", null, grid(albums.map(albumCard))));
    if (rest.length) parts.push(section("Singles and EPs", null, grid(rest.map(albumCard))));
    resultsEl.replaceChildren(...parts);
    setMessage(data.albums.length ? "" : "Spotify lists no releases for this artist.");
  } catch {
    if (token === searchToken) setMessage("Could not load that artist.");
  }
}

function backToResults() {
  kindsEl.hidden = false;
  if (lastResults) {
    searchToken++;
    showResults(lastResults);
  } else {
    painters = [];
    resultsEl.replaceChildren();
    setMessage("");
  }
}

/* --- one album, in a panel beside the results ------------------------------
   The Library's drawer, so an album opens the same way in both places. It
   used to replace the results, and Back ran the search again from scratch. */

let openAlbumId = null;

function closeAlbum() {
  if (!openAlbumId) return;
  openAlbumId = null;
  drawerPainters = [];
  drawerEl.hidden = true;
  drawerEl.replaceChildren();
}

function albumActions(album, box) {
  const job = albumJob(album);
  const group = job && jobGroup(job);
  const held = album.held_count;
  const total = album.tracks.length;
  const download = el("button", "", "Download album");
  download.type = "button";
  download.addEventListener("click", () => queue(album.url, `${album.artist} - ${album.name}`, album.cover));
  const note = el("span", "br-note");

  if (asked.has(album.url) || group === "active") {
    download.disabled = true;
    download.textContent = "Downloading…";
    note.textContent = job && job.items.length
      ? `${settledCount(job)} of ${job.items.length} tracks so far` : "Reading the album…";
  } else if (group === "done" || (total && held >= total)) {
    download.className = "ghost";
    download.textContent = "Download again";
    note.textContent = "Already in your library. Again makes a second copy.";
  } else if (held) {
    note.textContent = `${held} of ${total} are already in your library and would arrive as second copies. `
      + "To fetch only the rest, use the buttons below.";
  } else if (group === "attention") {
    note.textContent = "Some of the last attempt failed. Retry those from the Queue, or start again.";
  }
  box.replaceChildren(download, note);
}

function albumTrackRow(album, track) {
  const row = el("div", "br-trk");
  const name = el("span", "br-trk-name", track.name);
  if (track.artist && track.artist !== album.artist) {
    name.append(el("small", "", track.artist));
  }
  const state = el("span", "br-state");
  const card = { ...track, cover: album.cover };
  const { button, paint } = trackButton(card);
  row.append(el("span", "br-trk-no", track.track_no ?? ""), name, state,
             el("span", "br-dur", duration(track.duration_ms)), button);
  return painted(drawerPainters, row, () => {
    state.replaceChildren(stateBadge(trackState(card)));
    paint();
  });
}

async function openAlbum(id) {
  if (!id) return;
  openAlbumId = id;
  drawerPainters = [];
  const top = el("div", "lib-drawer-top");
  const close = el("button", "ghost", "✕ Close");
  close.type = "button";
  close.addEventListener("click", closeAlbum);
  top.append(close);
  const body = el("div", "lib-drawer-body");
  body.append(el("p", "empty", "Loading…"));
  drawerEl.replaceChildren(top, body);
  drawerEl.hidden = false;
  drawerEl.scrollTop = 0;
  close.focus({ preventScroll: true });

  let album;
  try {
    const response = await apiFetch(`/api/albums/${encodeURIComponent(id)}`);
    album = await response.json();
    if (openAlbumId !== id) return;
    if (!response.ok) {
      body.replaceChildren(el("p", "empty", album.detail || "Could not load that album."));
      return;
    }
  } catch {
    if (openAlbumId === id) body.replaceChildren(el("p", "empty", "Could not load that album."));
    return;
  }

  const hero = el("div", "lib-hero");
  const words = el("div", "lib-hero-text");
  const artist = el("button", "lib-artist-link", album.artist);
  artist.type = "button";
  artist.disabled = !album.artist_id;
  artist.addEventListener("click", () => openArtist(album.artist_id));
  const minutes = Math.round(album.tracks.reduce((n, t) => n + (t.duration_ms || 0), 0) / 60000);
  words.append(
    el("span", "lib-pill", releaseKind(album) || "Album"),
    el("h2", "lib-hero-title", album.name),
    artist,
    el("p", "lib-hero-meta",
       [album.year, plural(album.tracks.length, "track"), minutes ? `${minutes} min` : null]
         .filter(Boolean).join(" · ")));
  hero.append(remoteArt(album.cover), words);

  const actions = el("div", "lib-actions br-actions");
  painted(drawerPainters, actions, () => albumActions(album, actions));

  const tracks = el("div", "lib-tracks");
  tracks.append(...album.tracks.map((t) => albumTrackRow(album, t)));
  body.replaceChildren(hero, actions, tracks);
}

document.addEventListener("keydown", (event) => {
  if (pageEscape(event) && openAlbumId && !drawerEl.hidden) closeAlbum();
});

/* --- the box ---------------------------------------------------------------- */

function syncBox() {
  const text = input.value.trim();
  const url = looksLikeUrl(text);
  goEl.textContent = url ? "Download" : "Search";
  kindsEl.hidden = url;
  hintEl.hidden = !url;
  if (url) {
    const host = (/^(?:https?:\/\/)?([^/?#:]+\.[^/?#:]+)/i.exec(text) || [])[1] || "spotify.com";
    const what = /[/:]playlist/i.test(text) ? "A playlist"
      : /[/:]album/i.test(text) ? "An album"
      : /[/:]track|watch\?|youtu\.be/i.test(text) ? "A track" : "A link";
    hintEl.textContent = `${what} from ${host.replace(/^(www|open|music|m)\./i, "")}. `
      + "Press Download to queue it.";
  }
}

let typing = null;
input.addEventListener("input", () => {
  syncBox();
  syncForYou();
  clearTimeout(typing);
  const text = input.value.trim();
  if (!text) {
    searchToken++;
    lastResults = null;
    painters = [];
    resultsEl.replaceChildren();
    setMessage("");
    return;
  }
  if (looksLikeUrl(text) || text.length < 2) return;
  // Results follow the typing, the way the Library's search does - but each
  // one is a Spotify request, so only once the typing pauses.
  typing = setTimeout(runSearch, 450);
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearTimeout(typing);
  const text = input.value.trim();
  if (!text) return;
  if (!looksLikeUrl(text)) {
    kindsEl.hidden = false;
    runSearch();
    return;
  }
  goEl.disabled = true;
  try {
    if (await queue(text, "", null)) {
      input.value = "";
      syncBox();
    }
  } finally {
    goEl.disabled = false;
  }
});

kindsEl.querySelectorAll("button").forEach((button) => {
  button.addEventListener("click", () => setKind(button.dataset.kind));
});

libraryPicker(document.getElementById("browse-library"));
syncKinds();
syncBox();
syncForYou();

// Focus the search box only where a keyboard is already there. On a phone
// this summoned the on-screen one the instant the tab was tapped, covering
// half the screen and scrolling the page out from under the thumb that
// tapped it. Asked of the pointer rather than the width: a tablet in a
// wide window is still a touch device, and a laptop in a narrow one still
// has a real keyboard. Called by main.js when the Browse view is shown.
export function focusSearchIfPointer() {
  if (matchMedia("(hover: hover) and (pointer: fine)").matches) {
    input.focus();
  }
}
