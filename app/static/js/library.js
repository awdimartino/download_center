"use strict";

/* --- library ----------------------------------------------------------------
   Everything you own, three ways in:

   - Albums: a wall of covers (or a dense list), sorted and narrowed. A
     search finds songs as well as albums.
   - Artists: who made it. An artist's page splits their albums from their
     singles, which is where a record downloaded song by song shows up.
   - Needs attention: what wants a person, grouped by why - singles that
     belong together, covers with bars, albums nobody has reviewed.

   An album opens in a panel beside the list rather than expanding inside it.
   The old list folded the whole editor into each row, so the list was hard
   to read and the editor was hard to use.

   Select mode ticks albums and tracks for a bulk action, and the one that
   matters is Combine: several albums and loose tracks made into one album,
   in one step, with the order and cover chosen first. */

import { el, setNote, songRow } from "./core.js";
import { barRows } from "./charts.js";
import { setBadge } from "./nav.js";
import { registerOperation, startOperation } from "./operations.js";

const libraryView = document.getElementById("view-library");
const libraryEl = document.getElementById("library");
const libraryEmpty = document.getElementById("library-empty");
const libraryBadge = document.getElementById("library-badge");
const libraryCount = document.getElementById("library-count");
const libraryMore = document.getElementById("library-more");
const librarySearch = document.getElementById("library-search");
const libraryProgress = document.getElementById("library-progress");
const libraryGenresToggle = document.getElementById("library-genres-toggle");
const libraryGenresSection = document.getElementById("library-genres");
const libraryTabs = document.getElementById("library-tabs");
const libraryTodoCount = document.getElementById("library-todo-count");
const libraryKind = document.getElementById("library-kind");
const librarySort = document.getElementById("library-sort");
const libraryLayout = document.getElementById("library-layout");
const librarySelect = document.getElementById("library-select");
const libraryShow = document.getElementById("library-show");
const librarySuggest = document.getElementById("library-suggest");
const libraryDrawer = document.getElementById("library-drawer");
const libraryBar = document.getElementById("library-bar");
const libraryDialog = document.getElementById("library-dialog");
const libraryStatus = document.getElementById("library-status");

/* --- what is being looked at -------------------------------------------- */

// Per-viewer conveniences: which tab, how sorted, grid or list. Kept in the
// browser and allowed to vanish - a private window simply starts on Albums.
function remembered(key, fallback, allowed) {
  try {
    const value = localStorage.getItem(`library.${key}`);
    return value && allowed.includes(value) ? value : fallback;
  } catch {
    return fallback;
  }
}

function remember(key, value) {
  try { localStorage.setItem(`library.${key}`, value); } catch { /* fine */ }
}

const view = {
  tab: remembered("tab", "albums", ["albums", "artists", "todo"]),
  kind: remembered("kind", "all", ["all", "album", "single"]),
  sort: remembered("sort", "recent", ["recent", "artist", "album", "year", "plays"]),
  layout: remembered("layout", "grid", ["grid", "list"]),
  // Needs review, no MusicBrainz match, no ReplayGain - or none of them.
  show: "all",
  // The artist whose page is open, on the Artists tab.
  artist: null,
};

// One page. The server clamps at MAX_PAGE, so a refresh of more than that
// takes several requests.
const LIBRARY_PAGE = 60;
const LIBRARY_MAX_PAGE = 200;
let libraryShownCount = 0;
// The grid or list the current page of albums is appended to.
let listEl = null;

function albumKey(album) {
  return `${album.library_id}/${album.folder}`;
}

function albumName(album) {
  return [album.artist, album.album].filter(Boolean).join(" - ")
    || album.folder || "this album";
}

function plural(n, word) {
  return `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
}

async function getJSON(path, options) {
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  // fetch does not throw on 4xx or 5xx, and an error body is a {detail}
  // with nothing else - which used to fall through to "your library is
  // empty", the most alarming possible way to be wrong.
  if (!response.ok || data.available === false) {
    throw new Error(data.reason || data.detail
                    || `the server answered ${response.status}`);
  }
  return data;
}

function postJSON(path, body) {
  return getJSON(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

/* --- cover art ----------------------------------------------------------- */

// When each album's cover was last replaced here. The art URL is keyed on a
// track id, which does not change when the picture does, and it is cached
// for a week - so without a stamp the old cover stays on screen.
const artStamps = new Map();

// Navidrome resizes, and the embedded images behind these are often a
// megabyte each. Lazy, so scrolling past two thousand albums does not fetch
// two thousand covers, and removed if there is none rather than leaving a
// broken-image glyph.
function libraryArt(trackId, size, stamp, className = "") {
  const box = el("div", `art ${className}`.trim());
  if (!trackId) return box;
  const img = el("img");
  img.loading = "lazy";
  img.decoding = "async";
  img.alt = "";
  img.src = `/api/library/art?id=${encodeURIComponent(trackId)}&size=${size}`
    + (stamp ? `&v=${stamp}` : "");
  img.addEventListener("error", () => img.remove());
  box.append(img);
  return box;
}

function albumArt(album, size, className) {
  return libraryArt(album.art_id, size, artStamps.get(albumKey(album)), className);
}

/* --- selection ----------------------------------------------------------- */

// Kept across searches, tabs and pages, so a record's singles can be found
// one search at a time and combined at the end.
const selection = {
  on: false,
  albums: new Map(),   // albumKey -> album
  tracks: new Map(),   // track path -> { track, album }
};

function isPicked(album) {
  return selection.albums.has(albumKey(album));
}

function setSelecting(on) {
  selection.on = on;
  if (!on) {
    selection.albums.clear();
    selection.tracks.clear();
  }
  libraryView.classList.toggle("selecting", on);
  librarySelect.setAttribute("aria-pressed", String(on));
  librarySelect.textContent = on ? "Done" : "Select";
  refreshPicks();
  if (openAlbum) renderTracks();
}

function toggleAlbum(album) {
  const key = albumKey(album);
  if (selection.albums.has(key)) selection.albums.delete(key);
  else selection.albums.set(key, album);
  refreshPicks();
}

// Every card for an album shows whether it is picked - the same album can be
// on screen twice, in a grid and in a suggestion strip.
function refreshPicks() {
  libraryView.querySelectorAll("[data-key]").forEach((node) => {
    const picked = selection.albums.has(node.dataset.key);
    node.classList.toggle("picked", picked);
    node.setAttribute("aria-pressed", String(picked));
  });
  renderBar();
}

function looseTracks() {
  return [...selection.tracks.values()]
    .filter((t) => !selection.albums.has(albumKey(t.album)));
}

function renderBar() {
  if (!selection.on || !libraryDialog.hidden) {
    libraryBar.hidden = true;
    return;
  }
  const albums = selection.albums.size;
  const tracks = looseTracks().length;
  const parts = [albums && plural(albums, "album"), tracks && plural(tracks, "track")]
    .filter(Boolean);
  const what = el("span", "lib-bar-what",
    parts.length ? `${parts.join(" + ")} selected` : "Tap albums or tracks to select them");

  const combine = el("button", "", "Combine into album…");
  combine.type = "button";
  // Two tracks at least, which one album of twelve already is - combining
  // it with nothing would only rename it.
  combine.disabled = albums + tracks < 2;
  combine.addEventListener("click", () => openCombine());

  const square = el("button", "ghost", "Square covers");
  square.type = "button";
  square.disabled = !albums;
  square.title = "Trim the bars off every selected album's cover";
  square.addEventListener("click", () => squareCovers([...selection.albums.values()], square));

  const review = el("button", "ghost", "Mark reviewed");
  review.type = "button";
  review.disabled = !albums;
  review.addEventListener("click", () => markReviewed([...selection.albums.values()], review));

  const clear = el("button", "ghost", "Clear");
  clear.type = "button";
  clear.disabled = !albums && !tracks;
  clear.addEventListener("click", () => {
    selection.albums.clear();
    selection.tracks.clear();
    refreshPicks();
    if (openAlbum) renderTracks();
  });

  libraryBar.replaceChildren(what, combine, square, review, clear);
  libraryBar.hidden = false;
}

/* --- the album list ------------------------------------------------------ */

// Albums the cover survey found barred, by albumKey - so a card drawn before
// the survey answered can still be flagged when it does.
let barredKeys = new Set();

function isBarred(album) {
  return album.barred || barredKeys.has(albumKey(album));
}

function flags(album) {
  const box = el("span", "lib-flags");
  if (album.kind === "single") box.append(el("span", "lib-flag", "Single"));
  if (isBarred(album)) box.append(el("span", "lib-flag tone-warn", "Cover"));
  if (album.needs_review) box.append(el("span", "lib-flag tone-warn", "Review"));
  if (album.albums_here > 1) {
    box.append(el("span", "lib-flag tone-warn", `${album.albums_here} albums`));
  }
  return box;
}

// Run once in the background from the album list. The list cannot read a
// cover per row, so without this the flags only appeared after somebody had
// opened Needs attention.
// One survey in flight at a time. The list and Needs attention each asked
// for it, and opening one while the other was still waiting ran the whole
// survey twice over every album.
let coverSurveyRequest = null;
function fetchCoverSurvey() {
  if (!coverSurveyRequest) {
    coverSurveyRequest = getJSON("/api/library/attention/covers")
      .finally(() => { coverSurveyRequest = null; });
  }
  return coverSurveyRequest;
}

async function surveyCovers() {
  if (coverSurvey) return;
  try {
    coverSurvey = await fetchCoverSurvey();
  } catch {
    return;
  }
  barredKeys = new Set(coverSurvey.albums.map(albumKey));
  updateTodoCount();
  libraryEl.querySelectorAll(".lib-card, .lib-row").forEach((node) => {
    if (!barredKeys.has(node.dataset.key) || node.querySelector(".tone-warn")) return;
    node.querySelector(".lib-flags").append(el("span", "lib-flag tone-warn", "Cover"));
  });
}

function onAlbum(album) {
  if (selection.on) toggleAlbum(album);
  else openDrawer(album);
}

function clickable(node, album) {
  node.dataset.key = albumKey(album);
  node.tabIndex = 0;
  node.setAttribute("role", "button");
  node.addEventListener("click", () => onAlbum(album));
  node.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onAlbum(album);
    }
  });
  node.classList.toggle("picked", isPicked(album));
  return node;
}

function albumCard(album) {
  const card = el("div", "lib-card");
  const cover = el("div", "lib-card-cover");
  cover.append(albumArt(album, 300), flags(album), el("span", "lib-tick"));
  card.append(
    cover,
    el("span", "lib-card-title", album.album || album.folder),
    el("span", "lib-card-sub",
       [album.artist, album.year || null].filter(Boolean).join(" · ")));
  card.title = albumName(album);
  return clickable(card, album);
}

function albumListRow(album) {
  const row = el("div", "lib-row");
  const text = el("span", "lib-row-text");
  text.append(
    el("span", "lib-card-title", album.album || album.folder),
    el("span", "lib-card-sub",
       [album.artist, album.year || null, plural(album.tracks, "track")]
         .filter(Boolean).join(" · ")));
  row.append(el("span", "lib-tick"), albumArt(album, 96), text, flags(album),
             el("span", "lib-row-library", album.library || ""));
  return clickable(row, album);
}

function renderAlbum(album) {
  return view.layout === "list" ? albumListRow(album) : albumCard(album);
}

function freshList() {
  listEl = el("div", view.layout === "list" ? "lib-rows" : "lib-grid");
  return listEl;
}

function songsBlock(songs) {
  const block = el("section", "lib-songs");
  block.append(el("h3", "lib-section-title", "Songs"));
  for (const song of songs) {
    const row = songRow("button", libraryArt(song.id, 96), song.title,
                        [song.artist, song.album].filter(Boolean).join(" · "));
    row.type = "button";
    // Opens the album it is on, which is where anything about it is done.
    row.addEventListener("click", () => openDrawer({
      library_id: song.library_id, folder: song.folder, album: song.album,
      artist: song.artist, art_id: song.id, stub: true,
    }));
    block.append(row);
  }
  return block;
}

// How many covers the grid fits across right now, or 0 when that cannot be
// measured (the list layout, or the panel hidden behind another view).
function gridColumns() {
  if (view.layout === "list") return 0;
  const live = listEl && listEl.isConnected && listEl.classList.contains("lib-grid");
  const grid = live ? listEl : libraryEl.appendChild(el("div", "lib-grid"));
  const tracks = getComputedStyle(grid).gridTemplateColumns;
  if (!live) grid.remove();
  return tracks && tracks !== "none" ? tracks.split(" ").length : 0;
}

// A page size that ends on a full row. The column count follows the width,
// so a fixed page left a ragged last row: 120 albums seven across is
// seventeen rows and one album alone beside "Show more".
function fullRows(count, shown = 0) {
  const columns = gridColumns();
  if (!columns) return count;
  return Math.ceil((shown + count) / columns) * columns - shown;
}

async function fetchLibraryPage(offset, limit) {
  const query = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
    show: view.show,
    q: view.artist ? "" : librarySearch.value.trim(),
    kind: view.artist ? "all" : view.kind,
    sort: view.artist ? "year" : view.sort,
  });
  if (view.artist) query.set("artist", view.artist);
  return getJSON(`/api/library?${query}`);
}

// Three ways to load, because they differ in what they keep:
//   "reset"   - a new filter or search: the first page, from the top.
//   "more"    - the next page, appended.
//   "refresh" - after something changed: every album already on screen
//               read again, and the page kept where it was.
// Every edit used to reload with "reset", so fixing album 180 put you back
// at album 50 with "Show more" under your thumb.
export async function loadLibrary(mode = "reset", anchor = null) {
  syncControls();
  if (view.tab === "todo") return loadAttention();
  if (view.tab === "artists" && !view.artist) return loadArtists();
  return loadAlbums(mode, anchor);
}

// Which load is the latest. Typing a search starts one per pause, and a
// slow early answer used to arrive last and replace the list for what was
// typed after it - or append a stale "more" page to a new search.
let albumsRequest = 0;

async function loadAlbums(mode, anchor) {
  const mine = ++albumsRequest;
  const keepScroll = mode === "refresh";
  const anchorNode = anchor
    ? libraryEl.querySelector(`[data-key="${CSS.escape(anchor)}"]`) : null;
  const anchorTop = anchorNode ? anchorNode.getBoundingClientRect().top : null;
  const scrollY = window.scrollY;

  try {
    let albums = [];
    let data;
    if (mode === "more") {
      data = await fetchLibraryPage(libraryShownCount,
                                    fullRows(LIBRARY_PAGE, libraryShownCount));
      albums = data.albums || [];
    } else {
      // An artist page is one artist's records, which is never so many that
      // it needs paging - and splitting albums from singles needs them all.
      const want = view.artist ? LIBRARY_MAX_PAGE
        : keepScroll ? Math.max(libraryShownCount, fullRows(LIBRARY_PAGE))
        : fullRows(LIBRARY_PAGE);
      do {
        data = await fetchLibraryPage(albums.length,
                                      Math.min(want - albums.length, LIBRARY_MAX_PAGE));
        albums = albums.concat(data.albums || []);
      } while (albums.length < want && albums.length < data.total
               && (data.albums || []).length);
    }
    if (mine !== albumsRequest) return;

    if (mode === "more") {
      listEl.append(...albums.map(renderAlbum));
      libraryShownCount += albums.length;
    } else if (view.artist) {
      libraryEl.replaceChildren(...artistPage(view.artist, albums));
      libraryShownCount = albums.length;
    } else {
      const nodes = [];
      if (data.songs && data.songs.length) {
        nodes.push(songsBlock(data.songs));
        nodes.push(el("h3", "lib-section-title", "Albums"));
      }
      nodes.push(freshList());
      listEl.append(...albums.map(renderAlbum));
      libraryEl.replaceChildren(...nodes);
      libraryShownCount = albums.length;
    }

    if (keepScroll) {
      const again = anchor
        ? libraryEl.querySelector(`[data-key="${CSS.escape(anchor)}"]`) : null;
      if (again && anchorTop !== null) {
        window.scrollBy(0, again.getBoundingClientRect().top - anchorTop);
      } else {
        window.scrollTo(0, scrollY);
      }
    }

    // About the whole library, not the filtered page, so they do not move
    // when the filter does.
    libraryCount.textContent = [
      `${plural(data.albums_total, "album")} · ${plural(data.tracks, "track")}`,
      `${plural(data.review_albums, "album")} need${data.review_albums === 1 ? "s" : ""} review`,
      `${plural(data.unmatched_albums, "album")} with no MusicBrainz match`,
    ].join(" · ");
    libraryCount.hidden = !data.tracks || !!view.artist;

    const filtered = view.show !== "all" || view.kind !== "all"
      || librarySearch.value.trim();
    libraryEmpty.textContent = libraryShownCount || (data.songs || []).length
      ? ""
      : filtered ? "Nothing matches that." : "Nothing in your library yet.";
    libraryEmpty.hidden = !libraryEmpty.textContent;
    // A page that came back short means there is no more, however the total
    // compares - the list can change under you while you read it.
    libraryMore.hidden = !!view.artist || libraryShownCount >= data.total
      || !albums.length;
    // The menu badge counts what wants attention, not what exists - and
    // "needs review" is the count that can reach zero.
    setBadge(libraryBadge, data.review_albums);
    refreshPicks();
    showSuggestions();
    surveyCovers();
  } catch (err) {
    if (mine !== albumsRequest) return;
    if (mode !== "more") {
      libraryEl.replaceChildren();
      libraryShownCount = 0;
      libraryCount.hidden = true;
      libraryMore.hidden = true;
    }
    libraryEmpty.textContent = `Could not read your library: ${err.message}`;
    libraryEmpty.hidden = false;
  }
}

// After a change to one album: re-read what is on screen, keep that album
// where it was. Needs attention is re-read too, since the change was most
// likely the fix for something on it.
export function refreshLibrary(album) {
  attention = null;
  return loadLibrary("refresh", album ? albumKey(album) : null);
}

/* --- artists ------------------------------------------------------------- */

let artistsCache = null;

async function loadArtists() {
  librarySuggest.replaceChildren();
  libraryMore.hidden = true;
  libraryCount.hidden = true;
  if (!artistsCache) {
    libraryEl.replaceChildren(el("p", "empty", "Reading…"));
    try {
      artistsCache = (await getJSON("/api/library/artists")).artists;
    } catch (err) {
      libraryEl.replaceChildren();
      libraryEmpty.textContent = `Could not read your artists: ${err.message}`;
      libraryEmpty.hidden = false;
      return;
    }
  }
  renderArtists();
}

function mosaic(artIds, size) {
  const box = el("div", artIds.length >= 4 ? "lib-mosaic four" : "lib-mosaic");
  const ids = artIds.length >= 4 ? artIds.slice(0, 4) : artIds.slice(0, 1);
  box.append(...ids.map((id) => libraryArt(id, size)));
  return box;
}

function artistCounts(artist) {
  return [artist.albums && plural(artist.albums, "album"),
          artist.singles && plural(artist.singles, "single")]
    .filter(Boolean).join(" · ");
}

// Filtered and sorted here rather than asked for again: the whole list is
// one small request, and narrowing it as you type should not wait on the Pi.
function renderArtists() {
  const needle = librarySearch.value.trim().toLowerCase();
  const shown = artistsCache.filter((a) => !needle || a.artist.toLowerCase().includes(needle));
  const order = {
    plays: (a, b) => b.plays - a.plays,
    recent: (a, b) => (b.added > a.added ? 1 : b.added < a.added ? -1 : 0),
  }[view.sort] || ((a, b) => a.artist.localeCompare(b.artist));
  shown.sort(order);

  const grid = el("div", "lib-artists");
  for (const artist of shown) {
    const tile = el("button", "lib-artist");
    tile.type = "button";
    tile.append(mosaic(artist.art_ids, 150),
                el("span", "lib-card-title", artist.artist),
                el("span", "lib-card-sub", artistCounts(artist)));
    tile.addEventListener("click", () => openArtist(artist.artist));
    grid.append(tile);
  }
  libraryEl.replaceChildren(grid);
  libraryEmpty.textContent = shown.length ? "" : needle ? "No artist matches that."
    : "Nothing in your library yet.";
  libraryEmpty.hidden = !!shown.length;
}

function openArtist(name) {
  closeDrawer();
  view.tab = "artists";
  view.artist = name;
  view.show = "all";
  loadLibrary();
  window.scrollTo(0, 0);
}

function artistPage(name, albums) {
  const back = el("button", "ghost lib-back", "‹ All artists");
  back.type = "button";
  back.addEventListener("click", () => {
    view.artist = null;
    loadLibrary();
  });

  const full = albums.filter((a) => a.kind === "album");
  const singles = albums.filter((a) => a.kind === "single");
  const head = el("div", "lib-artist-head");
  const text = el("div");
  text.append(el("h2", "lib-artist-name", name),
              el("span", "lib-card-sub",
                 [full.length && plural(full.length, "album"),
                  singles.length && plural(singles.length, "single"),
                  plural(albums.reduce((n, a) => n + a.tracks, 0), "track")]
                   .filter(Boolean).join(" · ")));
  head.append(mosaic(albums.map((a) => a.art_id).filter(Boolean), 150), text);

  const nodes = [back, head];
  for (const group of suggestedGroups().filter((g) =>
    g.artist.toLowerCase() === name.toLowerCase())) {
    nodes.push(suggestionBanner(group));
  }
  if (full.length) {
    nodes.push(el("h3", "lib-section-title", "Albums"));
    const grid = el("div", "lib-grid");
    grid.append(...full.map(albumCard));
    nodes.push(grid);
  }
  if (singles.length) {
    const title = el("h3", "lib-section-title", "Singles");
    title.append(el("small", "", "Loose songs. Select a few to combine them into an album."));
    nodes.push(title);
    const grid = el("div", "lib-grid");
    grid.append(...singles.map(albumCard));
    nodes.push(grid);
  }
  return nodes;
}

/* --- needs attention ----------------------------------------------------- */

// The attention list, kept between visits to the tab and dropped whenever
// something changes the library.
let attention = null;
let coverSurvey = null;

// Groups somebody has said do not belong together. Keyed on exactly which
// singles were offered, so a new one arriving offers the group again.
function groupKey(group) {
  return [group.library_id, group.artist.toLowerCase(),
          ...group.albums.map((a) => a.folder).sort()].join("|");
}

function dismissed() {
  try {
    return new Set(JSON.parse(localStorage.getItem("library.dismissed") || "[]"));
  } catch {
    return new Set();
  }
}

function dismiss(group) {
  const set = dismissed();
  set.add(groupKey(group));
  remember("dismissed", JSON.stringify([...set]));
}

function suggestedGroups() {
  if (!attention) return [];
  const gone = dismissed();
  return attention.together.filter((g) => !gone.has(groupKey(g)));
}

async function fetchAttention() {
  if (!attention) attention = await getJSON("/api/library/attention");
  updateTodoCount();
  return attention;
}

function updateTodoCount() {
  if (!attention) return;
  const count = suggestedGroups().length + attention.beside.length
    + (coverSurvey ? coverSurvey.count : 0);
  libraryTodoCount.textContent = count ? count.toLocaleString() : "";
  libraryTodoCount.hidden = !count;
}

function stackOf(albums) {
  const box = el("span", "lib-stack");
  box.append(...albums.slice(0, 4).map((a) => albumArt(a, 96)));
  return box;
}

function suggestionBanner(group) {
  const banner = el("div", "lib-suggest");
  const text = el("span", "lib-suggest-text",
    `${plural(group.albums.length, "single")} by ${group.artist} look like one album`);
  text.append(el("small", "", group.albums.map((a) => a.album).join(", ")));

  const go = el("button", "", "Combine…");
  go.type = "button";
  go.addEventListener("click", () => combineGroup(group));
  const no = el("button", "ghost", "Not together");
  no.type = "button";
  no.addEventListener("click", () => {
    dismiss(group);
    banner.remove();
    updateTodoCount();
  });
  banner.append(stackOf(group.albums), text, go, no);
  return banner;
}

// On Albums, not narrowed: what arrived lately is what is most likely to be
// in pieces, so the suggestion sits at the top of what was just downloaded.
async function showSuggestions() {
  const quiet = view.tab !== "albums" || view.show !== "all" || view.kind === "album"
    || librarySearch.value.trim();
  if (quiet) {
    librarySuggest.replaceChildren();
    return;
  }
  try {
    await fetchAttention();
  } catch {
    return;
  }
  librarySuggest.replaceChildren(...suggestedGroups().slice(0, 2).map(suggestionBanner));
}

function miniCard(album, sub) {
  const card = el("div", "lib-mini");
  card.append(albumArt(album, 150), el("span", "lib-card-title", album.album),
              el("span", "lib-card-sub", sub || album.artist));
  return clickable(card, album);
}

function todoSection({ title, count, why, albums, actions = [], sub }) {
  const section = el("section", "lib-todo");
  const head = el("div", "lib-todo-head");
  const text = el("div", "lib-todo-text");
  const h = el("h3", "", title);
  if (count !== undefined) h.append(el("small", "", ` ${count.toLocaleString()}`));
  text.append(h, el("p", "", why));
  head.append(text);
  if (count === 0) head.append(el("span", "lib-done", "✓ All clear"));
  else head.append(...actions);
  section.append(head);
  if (albums && albums.length) {
    const strip = el("div", "lib-strip");
    strip.append(...albums.map((a) => miniCard(a, sub && sub(a))));
    section.append(strip);
  }
  return section;
}

function button(label, className, onClick) {
  const b = el("button", className, label);
  b.type = "button";
  b.addEventListener("click", () => onClick(b));
  return b;
}

async function loadAttention() {
  librarySuggest.replaceChildren();
  libraryMore.hidden = true;
  libraryCount.hidden = true;
  libraryEmpty.hidden = true;
  if (!attention) libraryEl.replaceChildren(el("p", "empty", "Reading…"));
  let data;
  try {
    data = await fetchAttention();
  } catch (err) {
    libraryEl.replaceChildren(el("p", "empty", `Could not read your library: ${err.message}`));
    return;
  }

  const groups = suggestedGroups();
  const together = el("section", "lib-todo");
  const head = el("div", "lib-todo-head");
  const text = el("div", "lib-todo-text");
  text.append(el("h3", "", "Singles that belong together"),
              el("p", "", "Songs downloaded one at a time arrive as an album each. "
                + "Two or more by one artist are offered here to combine."));
  head.append(text);
  if (!groups.length) head.append(el("span", "lib-done", "✓ All clear"));
  together.append(head);
  for (const group of groups) {
    const row = el("div", "lib-group");
    const words = el("span", "lib-suggest-text", group.albums.map((a) => a.album).join(", "));
    words.append(el("small", "", `${group.artist} · ${plural(group.albums.length, "single")}`));
    row.append(stackOf(group.albums), words,
      button("Combine…", "", () => combineGroup(group)),
      button("Not together", "ghost", () => {
        dismiss(group);
        loadAttention();
      }));
    together.append(row);
  }

  const covers = el("div");
  const nodes = [
    together,
    covers,
    todoSection({
      title: "Singles already on an album",
      count: data.beside.length,
      why: "The same song is on an album of that artist's you have. These are "
        + "duplicates rather than pieces of a record; open one to set it aside.",
      albums: data.beside,
      sub: (a) => `Also on ${a.on_album}`,
    }),
    todoSection({
      title: "Needs review",
      count: data.review.count,
      why: "Everything that arrives starts here. An album leaves when it gets a "
        + "MusicBrainz match, or when you mark it reviewed - some music is simply "
        + "not in MusicBrainz, and that is fine.",
      albums: data.review.albums,
      actions: [button("See all", "ghost", () => setShow("review"))],
    }),
    todoSection({
      title: "No ReplayGain",
      count: data.no_gain.count,
      why: "These play louder or quieter than everything else until measured.",
      albums: data.no_gain.albums,
      actions: [
        button("See all", "ghost", () => setShow("nogain")),
        button("Measure all", "", () => measureAll()),
      ],
    }),
  ];
  libraryEl.replaceChildren(...nodes);
  refreshPicks();
  showCoverSurvey(covers);
}

// Last, and by itself: it opens a file per album, which on the Pi is seconds
// the first time. The other sections are already readable by then.
async function showCoverSurvey(into) {
  if (!coverSurvey) {
    into.replaceChildren(todoSection({
      title: "Covers with bars",
      why: "Checking every album's cover…",
    }));
    try {
      coverSurvey = await fetchCoverSurvey();
      barredKeys = new Set(coverSurvey.albums.map(albumKey));
    } catch (err) {
      into.replaceChildren(todoSection({
        title: "Covers with bars", why: `Could not check: ${err.message}`,
      }));
      return;
    }
  }
  updateTodoCount();
  if (!into.isConnected) return;
  into.replaceChildren(todoSection({
    title: "Covers with bars",
    count: coverSurvey.count,
    why: "A YouTube video frame instead of album art. Squaring keeps the "
      + "picture and drops the bars, and changes nothing else.",
    albums: coverSurvey.albums.slice(0, 24),
    actions: [button("Square all", "", (b) =>
      squareCovers(coverSurvey.albums, b))],
  }));
}

function setShow(show) {
  closeDrawer();
  view.tab = "albums";
  view.artist = null;
  view.show = show;
  view.kind = "all";
  loadLibrary();
  window.scrollTo(0, 0);
}

function measureAll() {
  if (!confirm(
    "Measure ReplayGain for every album that has none?\n\n"
    + "Each album is measured as a whole, so album gain stays consistent, and "
    + "its files are rewritten with the new tags. On the Pi this can take a "
    + "long while; it can be stopped between albums.")) return;
  startOperation("replaygain", "/api/library/replaygain", {});
}

/* --- bulk actions -------------------------------------------------------- */

// One album at a time, so a failure on one is reported and the rest still
// happen - and so the Pi is never asked to rewrite a hundred albums at once.
async function eachAlbum(albums, button, verb, work) {
  button.disabled = true;
  const was = button.textContent;
  let done = 0;
  const failed = [];
  for (const album of albums) {
    button.textContent = `${verb} ${done + 1} of ${albums.length}…`;
    try {
      await work(album);
      done += 1;
    } catch (err) {
      failed.push(`${album.album}: ${err.message}`);
    }
  }
  button.textContent = was;
  button.disabled = false;
  return { done, failed };
}

async function squareCovers(albums, button) {
  let changed = 0;
  const { failed } = await eachAlbum(albums, button, "Squaring", async (album) => {
    const result = await postJSON("/api/library/cover/apply",
      { library_id: album.library_id, folder: album.folder, url: null });
    if (result.written || result.already_square) {
      barredKeys.delete(albumKey(album));
      album.barred = false;
    }
    if (result.written) {
      changed += 1;
      artStamps.set(albumKey(album), Date.now());
    }
  });
  setNote("library-op",
    `Squared ${plural(changed, "cover")}.`
    + (failed.length ? ` Could not: ${failed.join("; ")}` : "")
    + (changed ? " Navidrome shows them after its rescan." : ""),
    failed.length ? "warn" : "notice");
  coverSurvey = null;
  refreshLibrary();
}

async function markReviewed(albums, button) {
  const { done, failed } = await eachAlbum(albums, button, "Marking", (album) =>
    postJSON("/api/library/reviewed",
             { library_id: album.library_id, folder: album.folder, reviewed: true }));
  setNote("library-op",
    `Marked ${plural(done, "album")} reviewed.`
    + (failed.length ? ` Could not: ${failed.join("; ")}` : ""),
    failed.length ? "warn" : "notice");
  refreshLibrary();
}

/* --- choosing a match by hand ---------------------------------------------
   Beets refuses whenever it cannot tell two releases apart, which for a
   popular record means five near-identical pressings and no winner. It knows
   perfectly well what the candidates are; `quiet_fallback: skip` throws the
   list away. This asks for the list back and lets a person point at one. */

const candidatesEl = document.getElementById("candidates");
// What the pending lookup was for. The answer arrives over the websocket,
// by which time nothing in the message says which album asked.
let candidatesFor = null;

function closeCandidates() {
  candidatesFor = null;
  candidatesEl.replaceChildren();
  candidatesEl.hidden = true;
}

function candidateRow(album, candidate) {
  const row = el("div", "candidate");
  const title = candidate.title || "(untitled)";
  const detail = [
    candidate.artist,
    candidate.year || null,
    candidate.tracks ? `${candidate.tracks} tracks` : null,
    candidate.album || null,
  ].filter(Boolean).join(" · ");

  const use = el("button", "ghost primary", "Use this");
  use.addEventListener("click", () => useCandidate(album, candidate, use));

  row.append(
    el("div", "candidate-title", title),
    el("div", "candidate-detail", detail),
    // Beets' own number. Lower is closer; it is shown because the gap
    // between the first and second is usually the whole story.
    el("div", "candidate-distance", candidate.distance.toFixed(2)),
    use
  );
  if (candidate.penalties && candidate.penalties.length) {
    row.append(el("div", "candidate-why",
                  `held against it: ${candidate.penalties.join(", ")}`));
  }
  return row;
}

// Whether an answer (or the operation in flight) is about this album. Your
// lookups run one at a time, so the answer arriving can be an earlier
// album's, from this tab or another.
function sameAlbum(target, album) {
  return Boolean(target && album)
    && target.library_id === album.library_id && target.folder === album.folder;
}

function showCandidates(result) {
  const album = candidatesFor;
  if (!album || !sameAlbum(result, album)) return;
  const head = el("div", "candidates-head");
  head.append(el("span", "candidates-title", `Matches for ${albumName(album)}`));
  const close = el("button", "ghost", "Close");
  close.addEventListener("click", closeCandidates);
  head.append(close);

  const nodes = [head];
  if (result.error) {
    nodes.push(el("p", "candidates-empty", result.error));
  } else if (!result.candidates.length) {
    nodes.push(el("p", "candidates-empty",
      "MusicBrainz has nothing close enough to offer. The album stays as it "
      + "is, tagged the way it arrived."));
  } else {
    nodes.push(...result.candidates.map((c) => candidateRow(album, c)));
  }
  candidatesEl.replaceChildren(...nodes);
  candidatesEl.hidden = false;
}

async function askForCandidates(album, button) {
  candidatesFor = album;
  button.disabled = true;
  candidatesEl.replaceChildren(
    el("p", "candidates-empty",
       `Asking MusicBrainz about ${albumName(album)}…`));
  candidatesEl.hidden = false;
  const payload = await startOperation(
    "candidates", "/api/library/match",
    { library_id: album.library_id, folder: album.folder });
  button.disabled = false;
  if (!payload || payload.detail) {
    closeCandidates();
  } else if (!payload.started && !sameAlbum(payload.operation.target, album)) {
    closeCandidates();
    setNote("library-op",
      "MusicBrainz is already being asked about another album; "
      + "try again when that finishes.", "warn");
  }
}

async function useCandidate(album, candidate, button) {
  if (!confirm(
    `Tag "${albumName(album)}" as "${candidate.title}"`
    + `${candidate.artist ? ` by ${candidate.artist}` : ""}?\n\n`
    + "Every track in the folder is retagged. If the artist or album "
    + "changes they move to match, and the album keeps its identity — "
    + "stars and play counts survive.")) {
    return;
  }
  button.disabled = true;
  closeCandidates();
  closeDrawer();
  await startOperation("import", "/api/library/match/apply",
                       { library_id: album.library_id, folder: album.folder,
                         release_id: candidate.id });
}

/* --- quarantine ---------------------------------------------------------- */

// Reports a quarantine outcome the same way whether it moved one track or a
// whole album, so the two callers below cannot drift in wording.
function reportQuarantine(data) {
  const moved = data.quarantined || [];
  const failed = data.failed || [];
  const parts = [];
  if (moved.length) {
    parts.push(`Set aside ${plural(moved.length, "file")} to duplicates-removed/.`);
  }
  if (failed.length) parts.push(`Could not move: ${failed.join("; ")}`);
  setNote("library-op", parts.join(" ") || "Nothing was moved.",
         failed.length ? "warn" : "notice");
}

// Set aside every file in an album folder by hand - the wrong record
// entirely, not a worse copy of a right one.
async function quarantineAlbum(album, button) {
  if (!confirm(
    `Move ${plural(album.tracks, "file")} to duplicates-removed/ inside `
    + `${album.library || "this library"}?\n\n${albumName(album)}\n\n`
    + "This is for the wrong record entirely, not a worse copy of a right "
    + "one. It cannot be undone from here.")) return;

  button.disabled = true;
  try {
    const data = await postJSON("/api/library/quarantine", {
      library_id: album.library_id, folder: album.folder,
      album: album.album, artist: album.artist,
    });
    reportQuarantine(data);
    closeDrawer();
    refreshLibrary();
  } catch (err) {
    setNote("library-op", err.message, "warn");
    button.disabled = false;
  }
}

// Set aside one track by hand, leaving the rest of the album alone.
async function quarantineTrack(album, track, button, gone) {
  if (!confirm(
    `Move "${track.title}" to duplicates-removed/ inside `
    + `${album.library || "this library"}?\n\n${track.path}\n\n`
    + "This is for the wrong file entirely, not a worse copy of a right "
    + "one. It cannot be undone from here.")) return;

  button.disabled = true;
  try {
    const data = await postJSON("/api/library/track/quarantine", {
      library_id: album.library_id, folder: album.folder,
      track_id: track.id, album: album.album, artist: album.artist,
    });
    reportQuarantine(data);
    gone();
  } catch (err) {
    setNote("library-op", err.message, "warn");
    button.disabled = false;
  }
}

/* --- editing ------------------------------------------------------------- */

async function saveEdit(path, body, button, done) {
  button.disabled = true;
  const was = button.textContent;
  if (button.tagName === "BUTTON") button.textContent = "Saving…";
  setNote("library-op", "");
  try {
    done(await postJSON(path, body));
    return true;
  } catch (err) {
    setNote("library-op", err.message, "warn");
    return false;
  } finally {
    button.disabled = false;
    if (button.tagName === "BUTTON") button.textContent = was;
  }
}

function field(label, value, extra = {}) {
  const wrap = el("label", "edit-field");
  wrap.append(el("span", "edit-label", label));
  const input = el("input");
  input.type = extra.number ? "number" : "text";
  input.value = value ?? "";
  if (extra.number) input.min = "0";
  if (extra.wide) input.classList.add("wide");
  wrap.append(input);
  wrap.input = input;
  return wrap;
}

// Renaming an album is one action over every file in it, because one album
// is one UUID - the artist and the title are properties of the folder, and
// editing them on a single track is how a record becomes two.
function albumEditor(album) {
  const form = el("div", "album-edit");
  const artist = field("Album artist", album.artist, { wide: true });
  const name = field("Album", album.album, { wide: true });
  const save = el("button", "ghost primary", "Save album");

  save.addEventListener("click", async () => {
    const wantArtist = artist.input.value.trim();
    const wantAlbum = name.input.value.trim();
    if (!wantArtist || !wantAlbum) {
      setNote("library-op", "An artist and an album cannot be blank.", "warn");
      return;
    }
    if (wantArtist === album.artist && wantAlbum === album.album) return;
    if (!confirm(
      `Rename this album to "${wantArtist} — ${wantAlbum}"?\n\n`
      + `All ${plural(album.tracks, "track")} are retagged and the files move `
      + "to match.\nThe album keeps its identity, so stars and play counts "
      + "survive — unless an album of that name already exists, in which "
      + "case these join it.")) return;

    await saveEdit("/api/library/album/edit", {
      library_id: album.library_id, folder: album.folder,
      album_artist: wantArtist, album: wantAlbum,
    }, save, (data) => {
      setNote("library-op",
              `Renamed, and ${plural(data.moved, "file")} moved to match.`, "notice");
      closeDrawer();
      refreshLibrary(album);
    });
  });

  // Retagging to an album name that already exists is what merges into it,
  // but that meant retyping an existing artist and title exactly right - one
  // typo made a new album instead of joining the one you meant. This searches
  // the whole library and fills the two fields from a real match.
  const mergeLabel = el("span", "edit-label", "Merge into an existing album");
  const mergeInput = el("input", "album-merge-input");
  mergeInput.type = "search";
  mergeInput.autocomplete = "off";
  mergeInput.placeholder = "Search artist or album to merge into";
  const mergeResults = el("div", "album-merge-results");
  mergeResults.hidden = true;

  // Only the newest search's answer is drawn; see loadAlbums.
  let mergeRequest = 0;
  async function searchMergeTargets(query) {
    const mine = ++mergeRequest;
    let data;
    try {
      data = await getJSON(`/api/library?q=${encodeURIComponent(query)}&limit=8`);
    } catch (err) {
      if (mine !== mergeRequest) return;
      mergeResults.replaceChildren(
        el("p", "album-merge-empty", `Could not search: ${err.message}`));
      mergeResults.hidden = false;
      return;
    }
    if (mine !== mergeRequest) return;
    const matches = (data.albums || []).filter((a) => !(
      a.library_id === album.library_id && a.folder === album.folder));
    if (!matches.length) {
      mergeResults.replaceChildren(
        el("p", "album-merge-empty", "No other album matches."));
      mergeResults.hidden = false;
      return;
    }
    mergeResults.replaceChildren(...matches.map((a) => {
      const row = el("button", "album-merge-result");
      row.type = "button";
      row.append(
        el("span", "album-merge-name", albumName(a)),
        el("span", "album-merge-count", plural(a.tracks, "track")));
      row.addEventListener("click", () => {
        artist.input.value = a.artist;
        name.input.value = a.album;
        mergeInput.value = "";
        mergeResults.hidden = true;
        mergeResults.replaceChildren();
      });
      return row;
    }));
    mergeResults.hidden = false;
  }

  // Debounced: one request per pause, not one per keystroke.
  let mergeTimer = null;
  mergeInput.addEventListener("input", () => {
    clearTimeout(mergeTimer);
    const query = mergeInput.value.trim();
    if (query.length < 2) {
      mergeRequest += 1;  // so a search still in flight does not reopen it
      mergeResults.hidden = true;
      mergeResults.replaceChildren();
      return;
    }
    mergeTimer = setTimeout(() => searchMergeTargets(query), 250);
  });

  const merge = el("div", "album-merge");
  merge.append(mergeLabel, mergeInput, mergeResults);

  form.append(artist, name, save, merge);
  return form;
}

/* --- the cover, and nothing else -------------------------------------------
   "Find matches" brings art with it, but it also rewrites every tag and can
   move the files. Most of the time only the picture is wrong - a YouTube
   download arrives with the video frame, bars and all - so this offers
   covers and changes only that. */

// How long Navidrome takes to notice a changed file and serve its new art.
const RESCAN_WAIT_MS = 20000;

async function applyCover(album, candidate, button, picker) {
  button.disabled = true;
  try {
    const data = await postJSON("/api/library/cover/apply", {
      library_id: album.library_id, folder: album.folder, url: candidate.url,
    });
    const failed = data.failed || [];
    setNote("library-op",
      data.already_square ? "That cover is already square; nothing changed."
        : `New cover on ${plural(data.written, "track")}.`
          + (failed.length ? ` Could not change: ${failed.join("; ")}` : ""),
      failed.length ? "warn" : "notice");
    picker.hidden = true;
    picker.replaceChildren();
    barredKeys.delete(albumKey(album));
    album.barred = false;
    if (data.already_square) return;

    // Shown straight away from the choice itself, since Navidrome serves the
    // old picture until it has rescanned - then read back for real.
    const key = albumKey(album);
    libraryView.querySelectorAll(`[data-key="${CSS.escape(key)}"] .art img, .lib-hero .art img`)
      .forEach((img) => {
        if (img.closest(".lib-hero") && openAlbum && albumKey(openAlbum) !== key) return;
        img.src = candidate.preview;
      });
    coverSurvey = null;
    setTimeout(() => {
      artStamps.set(key, Date.now());
      refreshLibrary(album);
    }, RESCAN_WAIT_MS);
  } catch (err) {
    setNote("library-op", err.message, "warn");
    button.disabled = false;
  }
}

async function showCovers(album, button, picker) {
  if (!picker.hidden) {
    picker.hidden = true;
    picker.replaceChildren();
    return;
  }
  button.disabled = true;
  picker.replaceChildren(el("p", "cover-empty", "Looking for covers…"));
  picker.hidden = false;
  let data;
  try {
    data = await postJSON("/api/library/cover/candidates",
                          { library_id: album.library_id, folder: album.folder });
  } catch (err) {
    picker.replaceChildren(el("p", "cover-empty", `Could not look: ${err.message}`));
    return;
  } finally {
    button.disabled = false;
  }

  if (!data.candidates.length) {
    picker.replaceChildren(el("p", "cover-empty",
      "No covers found. The current one is already square, and nothing "
      + "else matched this album's name."));
    return;
  }
  picker.replaceChildren(...data.candidates.map((candidate) => {
    const choice = el("button", "cover-choice");
    choice.type = "button";
    choice.title = `Use this cover for ${albumName(album)}`;
    const img = el("img");
    img.alt = "";
    img.loading = "lazy";
    img.src = candidate.preview;
    // A Cover Art Archive link is offered without asking whether the
    // release has a front; one that does not just drops out of the list.
    img.addEventListener("error", () => choice.remove());
    choice.append(img, el("span", "cover-label", candidate.label),
                  el("span", "cover-detail", candidate.detail || ""));
    choice.addEventListener("click", () => applyCover(album, candidate, choice, picker));
    return choice;
  }));
}

/* --- one album, in the panel --------------------------------------------- */

let openAlbum = null;
let drawerTracks = [];
let drawerTracksEl = null;
let drawerTracksHead = null;
// Where the status block lives while no album is open. It is carried into
// the panel while one is, since the panel covers it.
const statusHome = document.createComment("library-status");
libraryStatus.before(statusHome);

function pill(text, tone = "") {
  return el("span", `lib-pill ${tone}`.trim(), text);
}

function statusPills(album) {
  const box = el("div", "lib-pills");
  // Opened from a song in the search results, which says nothing about the
  // album's state - better no pills than wrong ones.
  if (album.stub) return box;
  // Two separate questions. Whether MusicBrainz knows it is a fact about
  // MusicBrainz, and some music will never be in it. Whether it still needs
  // review is the queue: everything arrives needing it, and it leaves by
  // getting a match or by somebody marking it reviewed.
  if (album.matched) box.append(pill("MusicBrainz ✓", "tone-ok"));
  else if (album.partial) box.append(pill(`MusicBrainz: ${album.untagged} of ${album.tracks} unmatched`));
  else box.append(pill("No MusicBrainz match"));
  if (!album.matched) {
    box.append(album.reviewed ? pill("Reviewed ✓", "tone-ok")
                              : pill("Needs review", "tone-warn"));
  }
  box.append(album.no_gain ? pill("No ReplayGain", "tone-warn") : pill("ReplayGain ✓", "tone-ok"));
  if (isBarred(album)) box.append(pill("Cover has bars", "tone-warn"));
  // Renaming, matching, covers, combining and ReplayGain treat a folder as
  // one album, so the server refuses them here; this says so first.
  if (album.albums_here > 1) {
    box.append(pill(`${album.albums_here} albums in this folder — edit track by track`,
                    "tone-warn"));
  }
  return box;
}

function albumMeta(album) {
  const minutes = album.duration ? Math.round(album.duration / 60) : 0;
  const added = album.added ? new Date(album.added) : null;
  return [
    album.year || null,
    album.tracks ? plural(album.tracks, "track") : null,
    minutes ? `${minutes} min` : null,
    added && !Number.isNaN(added.getTime())
      ? `added ${added.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" })}`
      : null,
    album.plays ? plural(album.plays, "play") : null,
    album.library || null,
  ].filter(Boolean).join(" · ");
}

// The way out of the review queue for music MusicBrainz does not have.
// Only offered where there is something to decide - a match already took
// the album out - and undoable, because a tap on the wrong album should not
// lose it from the queue for good. The panel stays open: marking one and
// moving on to the next is the whole point.
function reviewButton(album, statusHolder) {
  if (album.matched || album.stub) return document.createTextNode("");
  const b = button("", "", () => saveEdit("/api/library/reviewed", {
    library_id: album.library_id, folder: album.folder,
    reviewed: !album.reviewed,
  }, b, () => {
    album.reviewed = !album.reviewed;
    album.needs_review = !album.reviewed;
    statusHolder.querySelector(".lib-pills").replaceWith(statusPills(album));
    refreshLibrary(album);
  // After, not inside: saveEdit puts the button's old words back when it
  // finishes, which would undo the new label.
  }).then(label));
  function label() {
    b.textContent = album.reviewed ? "Needs review" : "Mark reviewed";
    b.className = album.reviewed ? "ghost" : "";
    b.title = album.reviewed ? "Put this album back in the review queue"
      : "Tagged the way you want it, MusicBrainz or not";
  }
  label();
  return b;
}

function moreMenu(album) {
  const wrap = el("div", "lib-more");
  const toggle = el("button", "ghost dropdown", "More");
  toggle.type = "button";
  toggle.setAttribute("aria-haspopup", "true");
  toggle.setAttribute("aria-expanded", "false");
  const menu = el("div", "lib-menu");
  menu.hidden = true;

  const items = [];
  if (album.no_gain) {
    items.push(button("Measure ReplayGain", "ghost", () =>
      startOperation("replaygain", "/api/library/replaygain",
                     { library_id: album.library_id, folder: album.folder })));
  }
  items.push(button("Combine with other albums…", "ghost", () => {
    closeDrawer();
    setSelecting(true);
    selection.albums.set(albumKey(album), album);
    refreshPicks();
    setNote("library-op",
      `${albumName(album)} is selected. Pick the albums or tracks to combine `
      + "with it, then Combine.", "notice");
  }));
  items.push(button("Quarantine album", "ghost danger", (b) => quarantineAlbum(album, b)));
  menu.append(...items);

  toggle.addEventListener("click", (event) => {
    event.stopPropagation();
    menu.hidden = !menu.hidden;
    toggle.setAttribute("aria-expanded", String(!menu.hidden));
  });
  menu.addEventListener("click", () => { menu.hidden = true; });
  wrap.append(toggle, menu);
  return wrap;
}

function openDrawer(album) {
  openAlbum = album;
  closeCandidates();
  const top = el("div", "lib-drawer-top");
  const close = button("✕ Close", "ghost", () => closeDrawer());
  const pick = button(selection.on ? "Done selecting" : "Select tracks", "ghost", () => {
    setSelecting(!selection.on);
    pick.textContent = selection.on ? "Done selecting" : "Select tracks";
  });
  top.append(close, pick);

  const hero = el("div", "lib-hero");
  const words = el("div", "lib-hero-text");
  const artistLink = button(album.artist || "Unknown artist", "lib-artist-link",
                            () => openArtist(album.artist));
  words.append(
    pill(album.kind === "single" ? "Single" : "Album"),
    el("h2", "lib-hero-title", album.album || album.folder),
    artistLink,
    el("p", "lib-hero-meta", albumMeta(album)),
    statusPills(album));
  hero.append(albumArt(album, 600), words);

  const editWrap = el("div", "lib-panel");
  editWrap.hidden = true;
  const picker = el("div", "cover-picker");
  picker.hidden = true;

  const actions = el("div", "lib-actions");
  const edit = button("Edit details", "ghost", () => {
    if (album.tracks === undefined) {
      // A song-search stub before its tracks arrive: the names the editor
      // would start from are not the album's yet.
      setNote("library-op", "Still reading this album; try again in a moment.",
              "warn");
      return;
    }
    if (!editWrap.childElementCount) editWrap.append(albumEditor(album));
    editWrap.hidden = !editWrap.hidden;
    edit.textContent = editWrap.hidden ? "Edit details" : "Close editor";
  });
  const cover = button("Fetch cover", isBarred(album) ? "" : "ghost",
                       (b) => showCovers(album, b, picker));
  cover.title = "Replace only the cover art - every tag stays as it is";
  const match = button("Find matches", "ghost", (b) => askForCandidates(album, b));
  match.title = "Ask MusicBrainz, then retag every track from the one you pick";
  actions.append(reviewButton(album, words), edit, cover, match, moreMenu(album));

  drawerTracksHead = el("div", "lib-tracks-head");
  drawerTracksEl = el("div", "lib-tracks");
  drawerTracksEl.append(el("p", "empty", "Reading…"));

  const body = el("div", "lib-drawer-body");
  body.append(hero, libraryStatus, actions, editWrap, picker, drawerTracksHead, drawerTracksEl);
  libraryDrawer.replaceChildren(top, body);
  libraryDrawer.hidden = false;
  libraryView.classList.add("drawer-open");
  libraryDrawer.scrollTop = 0;
  close.focus({ preventScroll: true });
  loadTracks(album);
}

function closeDrawer() {
  if (!openAlbum) return;
  openAlbum = null;
  statusHome.after(libraryStatus);
  closeCandidates();
  libraryDrawer.hidden = true;
  libraryDrawer.replaceChildren();
  libraryView.classList.remove("drawer-open");
}

async function loadTracks(album) {
  const asked = album;
  try {
    const data = await getJSON(
      `/api/library/album?library_id=${album.library_id}`
      + `&folder=${encodeURIComponent(album.folder)}`);
    if (openAlbum !== asked) return;
    drawerTracks = data.items;
    // Opened from a song in search results, which knows less than a listing
    // row: fill in what the panel's header can now say - and the album's
    // own names, since the song only knew its track artist.
    if (album.tracks === undefined) album.tracks = data.items.length;
    if (album.stub) {
      album.artist = data.artist || album.artist;
      album.album = data.album || album.album;
    }
    renderTracks();
  } catch (err) {
    if (openAlbum !== asked) return;
    drawerTracksEl.replaceChildren(el("p", "empty", err.message));
  }
}

function renderTracks() {
  const album = openAlbum;
  if (!album || !drawerTracksEl) return;
  const allPicked = isPicked(album);
  drawerTracksHead.replaceChildren(el("h3", "lib-section-title", "Tracks"));
  if (selection.on && !allPicked) {
    drawerTracksHead.append(button("Select all", "ghost", () => {
      for (const track of drawerTracks) selection.tracks.set(track.path, { track, album });
      renderTracks();
      renderBar();
    }));
  }
  const nodes = [];
  for (const track of drawerTracks) {
    const { row, more } = trackRow(album, track, allPicked);
    nodes.push(row, more);
  }
  drawerTracksEl.replaceChildren(...nodes);
}

// The rarer edits for one track: its disc number, and moving it out of this
// album into another entirely, which is how a misfiled track is rescued and
// how one gets split off by mistake. Kept behind "More" so it cannot be
// triggered by the same accidental tap that would edit the title.
function trackMorePanel(album, track, titleInput, artistInput, gone) {
  const form = el("div", "track-edit");
  const disc = field("Disc", track.disc_no || "", { number: true });
  const discSave = el("button", "ghost primary", "Save disc");

  discSave.addEventListener("click", async () => {
    const d = parseInt(disc.input.value, 10);
    if (Number.isNaN(d) || d === (track.disc_no || 0)) return;
    await saveEdit("/api/library/track/edit",
      { library_id: album.library_id, path: track.path, disc_no: d },
      discSave, () => {
        track.disc_no = d;
        setNote("library-op", "Saved.", "notice");
      });
  });

  const moveArtist = field("Album artist", album.artist, { wide: true });
  const moveAlbum = field("Album", album.album, { wide: true });
  const single = el("button", "ghost", "As its own single");
  const moveSave = el("button", "ghost primary", "Move this track");

  // A single is an album of one, which is how Spotify presents it and how
  // the filer files it. Reads the title and artist fields on the row itself,
  // in case they have not been saved yet.
  single.addEventListener("click", () => {
    moveArtist.input.value = artistInput.value.trim()
      || track.artist || album.artist;
    moveAlbum.input.value = titleInput.value.trim() || track.title;
  });

  moveSave.addEventListener("click", async () => {
    const wantArtist = moveArtist.input.value.trim();
    const wantAlbum = moveAlbum.input.value.trim();
    if (!wantArtist || !wantAlbum) {
      setNote("library-op", "An artist and an album cannot be blank.", "warn");
      return;
    }
    if (!confirm(
      `Move "${track.title}" to "${wantArtist} — ${wantAlbum}"?\n\n`
      + "Only this track moves. It leaves this album and joins that one, "
      + "taking its own stars and play count with it.\n"
      + "Everything else in this album stays where it is.")) return;

    await saveEdit("/api/library/track/edit", {
      library_id: album.library_id, path: track.path,
      album_artist: wantArtist, album: wantAlbum,
    }, moveSave, () => {
      setNote("library-op", `"${track.title}" moved.`, "notice");
      gone();
    });
  });

  const moveWrap = el("div", "track-move");
  moveWrap.append(moveArtist, moveAlbum, single, moveSave);

  const quarantine = el("button", "ghost danger", "Quarantine this track");
  quarantine.addEventListener("click", () => quarantineTrack(album, track, quarantine, gone));

  form.append(disc, discSave, moveWrap, quarantine);
  return form;
}

// One track, edited in its row: number, title and artist save as soon as
// they lose focus with a changed value. Disc number and moving to another
// album are rarer, so they stay one tap away behind "More".
function trackRow(album, track, allPicked) {
  const row = el("div", `album-track${track.tagged ? "" : " unmatched"}`);

  const tick = el("input", "lib-track-tick");
  tick.type = "checkbox";
  tick.setAttribute("aria-label", `Select ${track.title}`);
  tick.checked = allPicked || selection.tracks.has(track.path);
  tick.disabled = allPicked;
  tick.addEventListener("change", () => {
    if (tick.checked) selection.tracks.set(track.path, { track, album });
    else selection.tracks.delete(track.path);
    row.classList.toggle("picked", tick.checked);
    renderBar();
  });
  row.classList.toggle("picked", tick.checked);

  const no = el("input", "track-no-input");
  no.type = "number";
  no.min = "0";
  no.value = track.track_no || "";
  no.setAttribute("aria-label", "Track number");

  const title = el("input", "track-title-input");
  title.type = "text";
  title.value = track.title || "";
  title.setAttribute("aria-label", "Title");

  const artist = el("input", "track-artist-input");
  artist.type = "text";
  artist.value = track.artist || "";
  artist.setAttribute("aria-label", "Artist");

  // The list and Needs attention are re-read behind the panel; the panel
  // keeps showing what was typed, since Navidrome has not rescanned yet and
  // reading it back would show the value from before the save.
  async function saveField(input, key, value, previous) {
    if (value === previous) return;
    // Setting the artist of a track with no album artist changes which
    // album the file is on, so the server may move it. Say so first. Asked
    // of the track: the album's artist falls back to the track artist, so
    // it was almost never empty and the file moved unannounced.
    if (key === "artist" && track.has_albumartist === false) {
      if (!confirm(
        `Set this track's artist to "${value}"?\n\n`
        + "It has no album artist, so this also decides which folder it "
        + "lives in and the file will move.")) {
        input.value = previous;
        return;
      }
    }
    await saveEdit("/api/library/track/edit",
      { library_id: album.library_id, path: track.path, [key]: value },
      input, (data) => {
        track[key] = value;
        if (data.path) track.path = data.path;
        if (data.moved) {
          // It is in another folder now; listing it here would offer edits
          // against an album it has left.
          drawerTracks = drawerTracks.filter((t) => t !== track);
          album.tracks = drawerTracks.length;
          renderTracks();
          setNote("library-op", `Saved, and moved to ${data.path}.`, "notice");
        } else {
          setNote("library-op", "Saved.", "notice");
        }
        refreshLibrary(album);
      });
  }

  no.addEventListener("change", () => {
    const n = parseInt(no.value, 10);
    if (Number.isNaN(n)) { no.value = track.track_no || ""; return; }
    saveField(no, "track_no", n, track.track_no || 0);
  });
  title.addEventListener("change", () => {
    saveField(title, "title", title.value.trim(), track.title || "");
  });
  artist.addEventListener("change", () => {
    saveField(artist, "artist", artist.value.trim(), track.artist || "");
  });

  // Moved or set aside: it is not on this album any more.
  const gone = () => {
    drawerTracks = drawerTracks.filter((t) => t !== track);
    selection.tracks.delete(track.path);
    renderTracks();
    renderBar();
    refreshLibrary(album);
  };

  const more = el("button", "ghost", "More…");
  const moreWrap = el("div", "track-editor");
  moreWrap.hidden = true;
  more.addEventListener("click", () => {
    if (!moreWrap.childElementCount) {
      moreWrap.append(trackMorePanel(album, track, title, artist, gone));
    }
    moreWrap.hidden = !moreWrap.hidden;
    more.textContent = moreWrap.hidden ? "More…" : "Less";
  });

  row.append(tick, no, title, artist, more);
  return { row, more: moreWrap };
}

/* --- combining into one album ---------------------------------------------
   Several albums and loose tracks into one, in one step. What used to take a
   rename per album, typed exactly right each time, and a move per loose
   track from behind "More". Nothing changes until Combine is pressed. */

let combining = null;

function mostCommon(values) {
  const counts = new Map();
  for (const v of values) if (v) counts.set(v, (counts.get(v) || 0) + 1);
  let best = "";
  let n = 0;
  for (const [v, c] of counts) if (c > n) { best = v; n = c; }
  return best;
}

function combineGroup(group) {
  closeDrawer();
  setSelecting(true);
  for (const album of group.albums) selection.albums.set(albumKey(album), album);
  refreshPicks();
  openCombine();
}

async function openCombine() {
  const albums = [...selection.albums.values()];
  const loose = looseTracks();
  const libraries = new Set([...albums.map((a) => a.library_id),
                             ...loose.map((t) => t.album.library_id)]);
  if (libraries.size > 1) {
    setNote("library-op",
      "Those are in different libraries. A combine works inside one library.", "warn");
    return;
  }

  libraryDialog.replaceChildren(dialogShell("Reading the tracks…"));
  libraryDialog.hidden = false;
  renderBar();

  let lists;
  try {
    lists = await Promise.all(albums.map((a) => getJSON(
      `/api/library/album?library_id=${a.library_id}&folder=${encodeURIComponent(a.folder)}`)));
  } catch (err) {
    closeCombine();
    setNote("library-op", `Could not read those albums: ${err.message}`, "warn");
    return;
  }

  // The album to keep goes first: the biggest real album selected, since
  // that is nearly always the record the rest belong on.
  const keep = albums.filter((a) => a.kind === "album")
    .sort((a, b) => b.tracks - a.tracks)[0] || null;
  const items = [];
  const ordered = keep ? [keep, ...albums.filter((a) => a !== keep)] : albums;
  for (const album of ordered) {
    const list = lists[albums.indexOf(album)];
    for (const track of list.items) items.push({ track, album });
  }
  items.push(...loose.map(({ track, album }) => ({ track, album })));
  if (items.length < 2) {
    closeCombine();
    setNote("library-op", "Choose at least two tracks to combine.", "warn");
    return;
  }

  const artist = mostCommon([...albums.map((a) => a.artist),
                             ...loose.map((t) => t.album.artist)]);
  combining = {
    library_id: [...libraries][0],
    albums, loose, items,
    target: keep ? albumKey(keep) : "new",
    newArtist: artist,
    newTitle: "",
    various: false,
    albumArtists: new Set([...albums.map((a) => a.artist),
                           ...loose.map((t) => t.album.artist)]).size,
    renumber: true,
    cover: keep ? { folder: keep.folder, art_id: keep.art_id } : null,
    guess: null,
    error: "",
  };
  if (!combining.cover && albums.length) {
    const first = albums.find((a) => !isBarred(a)) || albums[0];
    combining.cover = { folder: first.folder, art_id: first.art_id };
  }
  renderCombine();
  guessName();
}

// Asks Spotify which album most of these songs are on. Only ever fills an
// empty name and offers a cover; it never chooses anything by itself.
async function guessName() {
  const state = combining;
  const titles = [...new Set(state.items.map((i) => i.track.title))];
  try {
    const { guess } = await postJSON("/api/library/combine/guess",
                                     { artist: state.newArtist, titles });
    if (combining !== state || !guess) return;
    state.guess = guess;
    if (!state.newTitle) state.newTitle = guess.album;
    renderCombine();
  } catch {
    // Spotify not set up or not answering: the name is typed by hand.
  }
}

function closeCombine() {
  combining = null;
  libraryDialog.hidden = true;
  libraryDialog.replaceChildren();
  renderBar();
}

function dialogShell(message) {
  const sheet = el("div", "lib-sheet");
  sheet.setAttribute("role", "dialog");
  sheet.setAttribute("aria-label", "Combine into one album");
  sheet.append(el("p", "empty", message));
  return sheet;
}

function targetOption(checked, art, title, detail, onPick) {
  const option = el("label", `lib-target${checked ? " on" : ""}`);
  const radio = el("input");
  radio.type = "radio";
  radio.name = "combine-target";
  radio.checked = checked;
  radio.addEventListener("change", onPick);
  const words = el("span", "lib-target-text", title);
  words.append(el("small", "", detail));
  option.append(radio, art, words);
  return option;
}

function renderCombine() {
  const c = combining;
  if (!c) return;

  const sheet = el("div", "lib-sheet");
  sheet.setAttribute("role", "dialog");
  sheet.setAttribute("aria-label", "Combine into one album");

  const head = el("div", "lib-sheet-head");
  head.append(el("h2", "", "Combine into one album"),
              el("p", "", `${plural(c.items.length, "track")} from `
                + `${plural(new Set(c.items.map((i) => albumKey(i.album))).size, "album")}. `
                + "Nothing changes until you press Combine."));

  // 1. Where they end up.
  const step1 = el("section", "lib-step");
  step1.append(el("h3", "", "1 · Which album do they end up in?"));
  for (const album of c.albums.filter((a) => a.kind === "album")) {
    step1.append(targetOption(c.target === albumKey(album), albumArt(album, 96),
      album.album, `${album.artist} · keeps its name, stars and play counts`, () => {
        c.target = albumKey(album);
        c.cover = { folder: album.folder, art_id: album.art_id };
        renderCombine();
      }));
  }
  step1.append(targetOption(c.target === "new", el("span", "lib-plus", "+"),
    "A new album", "Or type the name of one you already have, and they join it", () => {
      c.target = "new";
      renderCombine();
    }));
  if (c.target === "new") {
    const fields = el("div", "lib-fields");
    const artist = field("Album artist", c.various ? "Various Artists" : c.newArtist, { wide: true });
    artist.input.disabled = c.various;
    artist.input.addEventListener("input", () => { c.newArtist = artist.input.value; updateSummary(); });
    const name = field("Album", c.newTitle, { wide: true });
    name.input.placeholder = "The album's name";
    name.input.addEventListener("input", () => { c.newTitle = name.input.value; updateSummary(); });
    fields.append(artist, name);
    step1.append(fields);
    if (c.guess) {
      const hint = el("p", "lib-note",
        `Spotify has ${plural(c.guess.votes, "of these song")} on `
        + `“${c.guess.album}”${c.guess.year ? ` (${c.guess.year})` : ""}.`);
      if (c.newTitle !== c.guess.album) {
        hint.append(" ", button("Use that name", "ghost", () => {
          c.newTitle = c.guess.album;
          renderCombine();
        }));
      }
      step1.append(hint);
    }
    if (c.albumArtists > 1) {
      const various = el("label", "lib-option");
      const box = el("input");
      box.type = "checkbox";
      box.checked = c.various;
      box.addEventListener("change", () => { c.various = box.checked; renderCombine(); });
      various.append(box, `These come from ${c.albumArtists} artists — file them as `
        + "Various Artists, keeping each track's own artist");
      step1.append(various);
    }
  }

  // 2. Order.
  const step2 = el("section", "lib-step");
  step2.append(el("h3", "", "2 · Track order"));
  const order = el("div", "lib-order");
  const titles = new Map();
  for (const i of c.items) {
    const t = i.track.title.toLowerCase();
    titles.set(t, (titles.get(t) || 0) + 1);
  }
  let dragFrom = null;
  c.items.forEach((item, index) => {
    const row = el("div", "lib-ord");
    row.draggable = true;
    const words = el("span", "lib-ord-title", item.track.title);
    if (titles.get(item.track.title.toLowerCase()) > 1) {
      words.append(el("span", "lib-dupe", " · same title twice"));
    }
    words.append(el("small", "", `from ${item.album.album}`));
    const up = button("↑", "ghost lib-move", () => move(index, index - 1));
    up.setAttribute("aria-label", `Move ${item.track.title} up`);
    up.disabled = index === 0;
    const down = button("↓", "ghost lib-move", () => move(index, index + 1));
    down.setAttribute("aria-label", `Move ${item.track.title} down`);
    down.disabled = index === c.items.length - 1;
    row.append(el("span", "lib-grip", "⋮⋮"),
               el("span", "lib-ord-no", String(c.renumber ? index + 1 : item.track.track_no || "")),
               words, up, down);
    row.addEventListener("dragstart", () => { dragFrom = index; row.classList.add("dragging"); });
    row.addEventListener("dragend", () => row.classList.remove("dragging"));
    row.addEventListener("dragover", (event) => { event.preventDefault(); row.classList.add("over"); });
    row.addEventListener("dragleave", () => row.classList.remove("over"));
    row.addEventListener("drop", (event) => {
      event.preventDefault();
      if (dragFrom !== null) move(dragFrom, index);
    });
    order.append(row);
  });
  function move(from, to) {
    if (to < 0 || to >= c.items.length || from === to) return;
    const [item] = c.items.splice(from, 1);
    c.items.splice(to, 0, item);
    renderCombine();
  }
  const renumber = el("label", "lib-option");
  const box = el("input");
  box.type = "checkbox";
  box.checked = c.renumber;
  box.addEventListener("change", () => { c.renumber = box.checked; renderCombine(); });
  renumber.append(box, `Number them 1–${c.items.length} in this order`);
  step2.append(order, renumber);
  if ([...titles.values()].some((n) => n > 1)) {
    step2.append(el("p", "lib-note tone-warn",
      "Two tracks share a title. Both are kept; the Duplicates panel can "
      + "choose between them afterwards."));
  }

  // 3. Cover.
  const step3 = el("section", "lib-step");
  step3.append(el("h3", "", "3 · Cover"));
  const covers = el("div", "lib-cover-pick");
  const coverChoice = (checked, art, label, choose) => {
    const option = el("label", "lib-cover-option");
    option.title = label;
    const radio = el("input");
    radio.type = "radio";
    radio.name = "combine-cover";
    radio.checked = checked;
    radio.addEventListener("change", choose);
    option.append(radio, art);
    return option;
  };
  for (const album of c.albums) {
    covers.append(coverChoice(
      !!c.cover && c.cover.folder === album.folder, albumArt(album, 150), album.album,
      () => { c.cover = { folder: album.folder, art_id: album.art_id }; }));
  }
  if (c.guess && c.guess.cover) {
    const img = el("div", "art");
    const pic = el("img");
    pic.alt = "";
    pic.src = c.guess.cover;
    img.append(pic);
    covers.append(coverChoice(!!c.cover && c.cover.url === c.guess.cover, img,
      `Spotify: ${c.guess.album}`, () => { c.cover = { url: c.guess.cover }; }));
  }
  step3.append(covers, el("p", "lib-note",
    "Whichever you pick goes on every track. A YouTube cover is squared first, "
    + "so none of them keep their bars."));

  // Footer.
  const foot = el("div", "lib-sheet-foot");
  const summary = el("span", "lib-sheet-summary");
  const go = button("", "", () => submitCombine(go));
  function updateSummary() {
    const name = destination();
    summary.classList.toggle("tone-warn", !!c.error);
    summary.textContent = c.error
      || (name.album ? `Into ${name.albumartist} — ${name.album}` : "Name the album");
    go.textContent = `Combine ${plural(c.items.length, "track")}`;
    go.disabled = !name.album || !name.albumartist;
  }
  foot.append(summary, button("Cancel", "ghost", () => closeCombine()), go);
  updateSummary();

  const body = el("div", "lib-sheet-body");
  body.append(step1, step2, step3);
  sheet.append(head, body, foot);
  const scroll = libraryDialog.querySelector(".lib-sheet-body");
  const top = scroll ? scroll.scrollTop : 0;
  libraryDialog.replaceChildren(sheet);
  body.scrollTop = top;
}

function destination() {
  const c = combining;
  const keepAlbum = c.albums.find((a) => albumKey(a) === c.target);
  if (keepAlbum) return { albumartist: keepAlbum.artist, album: keepAlbum.album };
  return {
    albumartist: (c.various ? "Various Artists" : c.newArtist).trim(),
    album: c.newTitle.trim(),
  };
}

async function submitCombine(go) {
  const c = combining;
  const name = destination();
  const keepAlbum = c.albums.find((a) => albumKey(a) === c.target);
  c.error = "";
  go.disabled = true;
  const payload = await startOperation("combine", "/api/library/combine", {
    library_id: c.library_id,
    albumartist: name.albumartist,
    album: name.album,
    albums: c.albums.map((a) => a.folder),
    tracks: c.loose.map((t) => t.track.path),
    keep: keepAlbum ? keepAlbum.folder : null,
    order: c.renumber ? c.items.map((i) => i.track.path) : [],
    cover_folder: c.cover && c.cover.folder ? c.cover.folder : null,
    cover_url: c.cover && c.cover.url ? c.cover.url : null,
  });
  if (!payload || payload.detail) {
    // Said in the dialog: the panel's own status line is behind it.
    c.error = (payload && payload.detail) || "Could not start the combine.";
    renderCombine();
    return;
  }
  closeCombine();
  setSelecting(false);
}

/* --- controls -------------------------------------------------------------- */

// Every control shows the state it is in, from one place, rather than each
// handler remembering to update the others.
function syncControls() {
  libraryTabs.querySelectorAll("[data-tab]").forEach((tab) => {
    tab.setAttribute("aria-selected", String(tab.dataset.tab === view.tab));
  });
  libraryKind.querySelectorAll("[data-kind]").forEach((chip) => {
    chip.setAttribute("aria-pressed", String(chip.dataset.kind === view.kind));
  });
  libraryLayout.querySelectorAll("[data-layout]").forEach((b) => {
    b.setAttribute("aria-pressed", String(b.dataset.layout === view.layout));
  });
  librarySort.value = view.sort;
  const albumsTab = view.tab === "albums";
  libraryKind.hidden = !albumsTab;
  libraryLayout.hidden = !albumsTab;
  librarySort.parentElement.hidden = view.tab === "todo" || !!view.artist;
  // Artists cannot be ticked - Select there read as "merge these artists",
  // which it is not. On one artist's page it ticks their albums as usual.
  librarySelect.hidden = view.tab === "artists" && !view.artist;
  libraryShow.value = view.show;
  libraryShow.parentElement.hidden = !albumsTab;
}

libraryTabs.addEventListener("click", (event) => {
  const tab = event.target.closest("[data-tab]");
  if (!tab) return;
  closeDrawer();
  view.tab = tab.dataset.tab;
  view.artist = null;
  if (view.tab !== "albums") view.show = "all";
  remember("tab", view.tab);
  loadLibrary();
});

libraryShow.addEventListener("change", () => {
  view.show = libraryShow.value;
  loadLibrary();
});

libraryKind.addEventListener("click", (event) => {
  const chip = event.target.closest("[data-kind]");
  if (!chip) return;
  view.kind = chip.dataset.kind;
  remember("kind", view.kind);
  loadLibrary();
});

librarySort.addEventListener("change", () => {
  view.sort = librarySort.value;
  remember("sort", view.sort);
  if (view.tab === "artists" && !view.artist) {
    if (artistsCache) renderArtists();
  } else {
    loadLibrary();
  }
});

libraryLayout.addEventListener("click", (event) => {
  const b = event.target.closest("[data-layout]");
  if (!b) return;
  view.layout = b.dataset.layout;
  remember("layout", view.layout);
  loadLibrary();
});

librarySelect.addEventListener("click", () => setSelecting(!selection.on));

libraryMore.addEventListener("click", () => {
  libraryMore.disabled = true;
  loadLibrary("more").finally(() => { libraryMore.disabled = false; });
});

// Debounced: one request per pause, not one per keystroke - each is a walk
// of Navidrome's whole index. Typing on an artist page or on Needs attention
// means looking for something, so it goes to the albums.
let librarySearchTimer = null;
librarySearch.addEventListener("input", () => {
  clearTimeout(librarySearchTimer);
  if (view.tab === "artists" && !view.artist) {
    if (artistsCache) renderArtists();
    return;
  }
  librarySearchTimer = setTimeout(() => {
    if (view.tab !== "albums") {
      view.tab = "albums";
      view.artist = null;
    }
    loadLibrary();
  }, 250);
});

// Escape backs out one layer at a time: the combine, then the panel, then
// select mode.
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || libraryView.hidden) return;
  if (!libraryDialog.hidden) closeCombine();
  else if (openAlbum) closeDrawer();
  else if (selection.on) setSelecting(false);
});

// A click anywhere else closes an open "More" menu.
document.addEventListener("click", () => {
  libraryView.querySelectorAll(".lib-menu").forEach((menu) => { menu.hidden = true; });
});

libraryDialog.addEventListener("click", (event) => {
  if (event.target === libraryDialog) closeCombine();
});

// A cheap grouped count, unlike the Duplicates panel's full scan - fetched
// fresh on every open rather than cached, and closed again on a second tap.
libraryGenresToggle.addEventListener("click", async () => {
  if (!libraryGenresSection.hidden) {
    libraryGenresSection.hidden = true;
    return;
  }
  libraryGenresSection.hidden = false;
  libraryGenresSection.replaceChildren(el("p", "empty", "Loading…"));
  try {
    const data = await getJSON("/api/library/genres");
    const rows = barRows(data.genres || [], (g) => g.genre, (g) => g.tracks,
                         "No genres tagged yet.");
    if (data.untagged) {
      rows.push(el("p", "panel-sub",
        `${plural(data.untagged, "track")} with no genre tag.`));
    }
    libraryGenresSection.replaceChildren(...rows);
  } catch (err) {
    libraryGenresSection.replaceChildren(
      el("p", "empty", `Could not read genres: ${err.message}`));
  }
});

/* --- long-running operations ------------------------------------------------ */

function showProgress(operation, label) {
  if (operation.status !== "running") {
    libraryProgress.replaceChildren();
    libraryProgress.hidden = true;
    return;
  }
  const p = operation.progress;
  const text = p ? `${label}: ${p.done + (label === "ReplayGain" ? 1 : 0)} of ${p.total} — ${p.album}`
    : `${label}: starting…`;
  const nodes = [el("span", "banner-text", text)];
  if (label === "ReplayGain") {
    const stop = el("button", "ghost", operation.stopping ? "Stopping…" : "Stop");
    stop.type = "button";
    stop.disabled = !!operation.stopping;
    stop.addEventListener("click", async () => {
      stop.disabled = true;
      stop.textContent = "Stopping…";
      const response = await fetch("/api/library/replaygain/stop", { method: "POST" })
        .catch(() => null);
      if (!response || !response.ok) {
        const data = response ? await response.json().catch(() => ({})) : {};
        setNote("library-op", data.detail || "Could not ask it to stop.", "warn");
        stop.disabled = false;
        stop.textContent = "Stop";
      }
    });
    nodes.push(stop);
  }
  libraryProgress.replaceChildren(...nodes);
  libraryProgress.hidden = false;
}

function gainSummary(result) {
  const parts = [`ReplayGain measured for ${plural(result.measured, "album")}`
                 + (result.total > 1 ? ` of ${result.total}` : "") + "."];
  if (result.stopped) parts.push("Stopped on request.");
  if (result.failures) {
    parts.push(`${result.failures} failed: ${result.failed.join("; ")}`);
  }
  if (result.skipped && result.skipped.length) {
    parts.push(`Skipped: ${result.skipped.join("; ")}`);
  }
  // Navidrome shows the new values after the scan it has been asked for.
  if (result.measured) parts.push("Navidrome picks it up on its next scan.");
  return [parts.join(" "), result.failures ? "warn" : "notice"];
}

function importSummary(result) {
  const failed = result.failed || [];
  if (failed.length) return [`Retagging problems: ${failed.join("; ")}`, "warn"];
  if (result.imported) {
    return ["Retagged. It keeps the album identity it had, so nothing "
            + "starred was lost.", "notice"];
  }
  // Nothing changed and nothing failed: beets ran and refused. Said plainly,
  // because the album looking untouched is exactly how this used to hide.
  if (result.skipped) {
    return ["That release did not tag it; the album is unchanged.", "warn"];
  }
  return ["", "notice"];
}

// The album's own buttons run the same beets lock, so they cannot be live
// while a retag is in flight.
registerOperation("import", {
  note: "library-op",
  onButtonState(running) {
    libraryDrawer.querySelectorAll(".lib-actions button").forEach((b) => { b.disabled = running; });
  },
  onResult(result) {
    if (result.busy) {
      setNote("library-op",
              "An import is already running; this one was not started.", "warn");
    } else if (!result.ran) {
      setNote("library-op", `Nothing to import: ${result.reason}`, "warn");
    } else {
      const [message, tone] = importSummary(result);
      setNote("library-op", message, tone);
    }
    refreshLibrary();
  },
});

// No button of its own: it is started from an album, and its result is a
// list rather than a message.
registerOperation("candidates", {
  note: "library-op",
  onFailed() {
    closeCandidates();
  },
  onResult(result) {
    showCandidates(result);
  },
});

registerOperation("replaygain", {
  note: "library-op",
  onButtonState(running, operation) {
    showProgress(operation, "ReplayGain");
  },
  onResult(result) {
    const [message, tone] = gainSummary(result);
    setNote("library-op", message, tone);
    refreshLibrary();
  },
});

registerOperation("combine", {
  note: "library-op",
  onButtonState(running, operation) {
    showProgress(operation, "Combining");
  },
  onResult(result) {
    const failed = result.failed || [];
    setNote("library-op",
      `Combined ${plural(result.moved || 0, "track")} into `
      + `${result.albumartist} — ${result.album}.`
      + (failed.length ? ` Problems: ${failed.join("; ")}` : "")
      + " Navidrome shows it as one album after its rescan.",
      failed.length ? "warn" : "notice");
    artistsCache = null;
    coverSurvey = null;
    refreshLibrary();
  },
});

// The list is read from Navidrome's database, which refreshes on scan, so it
// can be a few minutes behind the disk. That is fine for a page somebody
// opens deliberately and not fine when they have just fixed something.
document.getElementById("library-rescan").addEventListener("click", async () => {
  const rescan = document.getElementById("library-rescan");
  rescan.disabled = true;
  rescan.textContent = "Scanning…";
  try {
    const payload = await fetch("/api/library/rescan", { method: "POST" })
      .then((r) => r.json());
    setNote("library-op", payload.detail || "", payload.detail ? "warn" : "");
  } catch (err) {
    setNote("library-op", `Could not ask for a scan: ${err.message}`, "warn");
  } finally {
    rescan.disabled = false;
    rescan.textContent = "Rescan";
    artistsCache = null;
    refreshLibrary();
  }
});

syncControls();
