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
   in one step, with the order and cover chosen first.

   This module is the list - albums, artists, select mode and the controls -
   and the panel's entry point. Needs attention, the album panel and Combine
   are library-attention.js, library-drawer.js and library-combine.js; what
   they share is library-shared.js. */

import { apiFetch, el, setNote, songRow } from "./core.js";
import { barRows } from "./charts.js";
import { setBadge } from "./nav.js";
import {
  actionButton,
  albumArt,
  albumKey,
  albumName,
  getJSON,
  isBarred,
  isPicked,
  libraryArt,
  libraryBadge,
  libraryBar,
  libraryCount,
  libraryDialog,
  libraryEl,
  libraryEmpty,
  libraryGenresSection,
  libraryGenresToggle,
  libraryKind,
  libraryLayout,
  libraryMore,
  librarySearch,
  librarySelect,
  libraryShow,
  librarySort,
  libraryState,
  librarySuggest,
  libraryTabs,
  libraryView,
  loadCoverSurvey,
  looseTracks,
  plural,
  remember,
  selection,
  viewing
} from "./library-shared.js";
import {
  loadAttention,
  markReviewed,
  showSuggestions,
  squareCovers,
  suggestedGroups,
  suggestionBanner,
  updateTodoCount
} from "./library-attention.js";
import {
  closeDrawer,
  openAlbum,
  openDrawer,
  renderTracks
} from "./library-drawer.js";
import { closeCombine, openCombine } from "./library-combine.js";

/* --- select mode --------------------------------------------------------- */

export function setSelecting(on) {
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
export function refreshPicks() {
  libraryView.querySelectorAll("[data-key]").forEach((node) => {
    const picked = selection.albums.has(node.dataset.key);
    node.classList.toggle("picked", picked);
    node.setAttribute("aria-pressed", String(picked));
  });
  renderBar();
}

export function renderBar() {
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

  const combine = actionButton("Combine into album…", "", () => openCombine());
  // Two tracks at least, which one album of twelve already is - combining
  // it with nothing would only rename it.
  combine.disabled = albums + tracks < 2;

  const square = actionButton("Square covers", "ghost",
                        (b) => squareCovers([...selection.albums.values()], b));
  square.disabled = !albums;
  square.title = "Trim the bars off every selected album's cover";

  const review = actionButton("Mark reviewed", "ghost",
                        (b) => markReviewed([...selection.albums.values()], b));
  review.disabled = !albums;

  const clear = actionButton("Clear", "ghost", () => {
    selection.albums.clear();
    selection.tracks.clear();
    refreshPicks();
    if (openAlbum) renderTracks();
  });
  clear.disabled = !albums && !tracks;

  libraryBar.replaceChildren(what, combine, square, review, clear);
  libraryBar.hidden = false;
}

/* --- the album list ------------------------------------------------------ */

// One page. The server clamps at MAX_PAGE, so a refresh of more than that
// takes several requests.
const LIBRARY_PAGE = 60;
const LIBRARY_MAX_PAGE = 200;
let libraryShownCount = 0;
// The grid or list the current page of albums is appended to.
let listEl = null;

function flags(album) {
  const box = el("span", "lib-flags");
  if (album.kind === "single") box.append(el("span", "lib-flag", "Single"));
  if (isBarred(album)) box.append(el("span", "lib-flag tone-warn flag-cover", "Cover"));
  if (album.needs_review) box.append(el("span", "lib-flag tone-warn", "Review"));
  if (album.albums_here > 1) {
    box.append(el("span", "lib-flag tone-warn", `${album.albums_here} albums`));
  }
  return box;
}

// Run once in the background from the album list. The list cannot read a
// cover per row, so without this the flags only appeared after somebody had
// opened Needs attention.
async function surveyCovers() {
  if (libraryState.coverSurvey) return;
  try {
    await loadCoverSurvey();
  } catch {
    return;
  }
  updateTodoCount();
  libraryEl.querySelectorAll(".lib-card, .lib-row").forEach((node) => {
    // Its own flag, not any warning: a card already flagged Review never
    // got the Cover flag beside it.
    if (!libraryState.barredKeys.has(node.dataset.key) || node.querySelector(".flag-cover")) return;
    node.querySelector(".lib-flags").prepend(el("span", "lib-flag tone-warn flag-cover", "Cover"));
  });
}

function onAlbum(album) {
  if (selection.on) toggleAlbum(album);
  else openDrawer(album);
}

export function clickable(node, album) {
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
  return viewing.layout === "list" ? albumListRow(album) : albumCard(album);
}

function freshList() {
  listEl = el("div", viewing.layout === "list" ? "lib-rows" : "lib-grid");
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
  if (viewing.layout === "list") return 0;
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
    show: viewing.show,
    q: viewing.artist ? "" : librarySearch.value.trim(),
    kind: viewing.artist ? "all" : viewing.kind,
    sort: viewing.artist ? "year" : viewing.sort,
  });
  if (viewing.artist) query.set("artist", viewing.artist);
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
  if (viewing.tab === "todo") return loadAttention();
  if (viewing.tab === "artists" && !viewing.artist) return loadArtists();
  return loadAlbums(mode, anchor);
}

// Typing a search starts one load per pause, and a slow early answer used to
// arrive last and replace the list for what was typed after it - or append a
// stale "more" page to a new search. `libraryState.load` says which is the
// latest, shared with the other tabs' loaders.
async function loadAlbums(mode, anchor) {
  const mine = ++libraryState.load;
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
      // An artist page is drawn whole - splitting albums from singles needs
      // every record - so it reads page after page until it has them all.
      // It used to ask for one page and stop at 200 without a word.
      const want = viewing.artist ? Infinity
        : keepScroll ? Math.max(libraryShownCount, fullRows(LIBRARY_PAGE))
        : fullRows(LIBRARY_PAGE);
      do {
        data = await fetchLibraryPage(albums.length,
                                      Math.min(want - albums.length, LIBRARY_MAX_PAGE));
        albums = albums.concat(data.albums || []);
      } while (albums.length < want && albums.length < data.total
               && (data.albums || []).length);
    }
    if (mine !== libraryState.load) return;

    if (mode === "more") {
      listEl.append(...albums.map(renderAlbum));
      libraryShownCount += albums.length;
    } else if (viewing.artist) {
      libraryEl.replaceChildren(...artistPage(viewing.artist, albums));
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
    libraryCount.hidden = !data.tracks || !!viewing.artist;

    const filtered = viewing.show !== "all" || viewing.kind !== "all"
      || librarySearch.value.trim();
    libraryEmpty.textContent = libraryShownCount || (data.songs || []).length
      ? ""
      : filtered ? "Nothing matches that." : "Nothing in your library yet.";
    libraryEmpty.hidden = !libraryEmpty.textContent;
    // A page that came back short means there is no more, however the total
    // compares - the list can change under you while you read it.
    libraryMore.hidden = !!viewing.artist || libraryShownCount >= data.total
      || !albums.length;
    // The menu badge counts what wants attention, not what exists - and
    // "needs review" is the count that can reach zero.
    setBadge(libraryBadge, data.review_albums);
    refreshPicks();
    showSuggestions();
    surveyCovers();
  } catch (err) {
    if (mine !== libraryState.load) return;
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
  libraryState.attention = null;
  return loadLibrary("refresh", album ? albumKey(album) : null);
}

/* --- artists ------------------------------------------------------------- */

async function loadArtists() {
  const mine = ++libraryState.load;
  librarySuggest.replaceChildren();
  libraryMore.hidden = true;
  libraryCount.hidden = true;
  if (!libraryState.artistsCache) {
    libraryEl.replaceChildren(el("p", "empty", "Reading…"));
    try {
      const artists = (await getJSON("/api/library/artists")).artists;
      libraryState.artistsCache = artists;
      if (mine !== libraryState.load) return;
    } catch (err) {
      if (mine !== libraryState.load) return;
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
  const shown = libraryState.artistsCache.filter((a) => !needle || a.artist.toLowerCase().includes(needle));
  const order = {
    plays: (a, b) => b.plays - a.plays,
    recent: (a, b) => (b.added > a.added ? 1 : b.added < a.added ? -1 : 0),
  }[viewing.sort] || ((a, b) => a.artist.localeCompare(b.artist));
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

export function openArtist(name) {
  closeDrawer();
  viewing.tab = "artists";
  viewing.artist = name;
  viewing.show = "all";
  loadLibrary();
  window.scrollTo(0, 0);
}

function artistPage(name, albums) {
  const back = el("button", "ghost lib-back", "‹ All artists");
  back.type = "button";
  back.addEventListener("click", () => {
    viewing.artist = null;
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

/* --- controls -------------------------------------------------------------- */

// Every control shows the state it is in, from one place, rather than each
// handler remembering to update the others.
function syncControls() {
  libraryTabs.querySelectorAll("[data-tab]").forEach((tab) => {
    tab.setAttribute("aria-selected", String(tab.dataset.tab === viewing.tab));
  });
  libraryKind.querySelectorAll("[data-kind]").forEach((chip) => {
    chip.setAttribute("aria-pressed", String(chip.dataset.kind === viewing.kind));
  });
  libraryLayout.querySelectorAll("[data-layout]").forEach((b) => {
    b.setAttribute("aria-pressed", String(b.dataset.layout === viewing.layout));
  });
  librarySort.value = viewing.sort;
  const albumsTab = viewing.tab === "albums";
  libraryKind.hidden = !albumsTab;
  libraryLayout.hidden = !albumsTab;
  librarySort.parentElement.hidden = viewing.tab === "todo" || !!viewing.artist;
  // Artists cannot be ticked - Select there read as "merge these artists",
  // which it is not. On one artist's page it ticks their albums as usual.
  librarySelect.hidden = viewing.tab === "artists" && !viewing.artist;
  libraryShow.value = viewing.show;
  libraryShow.parentElement.hidden = !albumsTab;
}

libraryTabs.addEventListener("click", (event) => {
  const tab = event.target.closest("[data-tab]");
  if (!tab) return;
  closeDrawer();
  viewing.tab = tab.dataset.tab;
  viewing.artist = null;
  if (viewing.tab !== "albums") viewing.show = "all";
  remember("tab", viewing.tab);
  loadLibrary();
});

libraryShow.addEventListener("change", () => {
  viewing.show = libraryShow.value;
  loadLibrary();
});

libraryKind.addEventListener("click", (event) => {
  const chip = event.target.closest("[data-kind]");
  if (!chip) return;
  viewing.kind = chip.dataset.kind;
  remember("kind", viewing.kind);
  loadLibrary();
});

librarySort.addEventListener("change", () => {
  viewing.sort = librarySort.value;
  remember("sort", viewing.sort);
  if (viewing.tab === "artists" && !viewing.artist) {
    if (libraryState.artistsCache) renderArtists();
  } else {
    loadLibrary();
  }
});

libraryLayout.addEventListener("click", (event) => {
  const b = event.target.closest("[data-layout]");
  if (!b) return;
  viewing.layout = b.dataset.layout;
  remember("layout", viewing.layout);
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
  if (viewing.tab === "artists" && !viewing.artist) {
    if (libraryState.artistsCache) renderArtists();
    return;
  }
  librarySearchTimer = setTimeout(() => {
    if (viewing.tab !== "albums") {
      viewing.tab = "albums";
      viewing.artist = null;
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

// The list is read from Navidrome's database, which refreshes on scan, so it
// can be a few minutes behind the disk. That is fine for a page somebody
// opens deliberately and not fine when they have just fixed something.
document.getElementById("library-rescan").addEventListener("click", async () => {
  const rescan = document.getElementById("library-rescan");
  rescan.disabled = true;
  rescan.textContent = "Scanning…";
  try {
    const payload = await apiFetch("/api/library/rescan", { method: "POST" })
      .then((r) => r.json());
    setNote("library-op", payload.detail || "", payload.detail ? "warn" : "");
  } catch (err) {
    setNote("library-op", `Could not ask for a scan: ${err.message}`, "warn");
  } finally {
    rescan.disabled = false;
    rescan.textContent = "Rescan";
    libraryState.artistsCache = null;
    refreshLibrary();
  }
});

syncControls();
