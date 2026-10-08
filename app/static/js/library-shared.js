"use strict";

/* --- library: what its modules share ------------------------------------
   The Library panel is five modules. This one is the leaf: the panel's
   elements, what is being looked at, the small helpers, cover art, the
   selection and the state more than one module replaces. It imports
   nothing from the others, so it is always evaluated first.

   The other four import each other's functions freely - the list opens the
   panel, the panel reloads the list. That cycle is safe because only
   function declarations cross it, and those exist before any module body
   runs; nothing at a module's top level may use another library module's
   `const` or `let`. */

import { apiFetch, el, getJSON, setNote } from "./core.js";

export const libraryView = document.getElementById("view-library");
export const libraryEl = document.getElementById("library");
export const libraryEmpty = document.getElementById("library-empty");
export const libraryBadge = document.getElementById("library-badge");
export const libraryCount = document.getElementById("library-count");
export const libraryMore = document.getElementById("library-more");
export const librarySearch = document.getElementById("library-search");
const libraryProgress = document.getElementById("library-progress");
export const libraryGenresToggle = document.getElementById("library-genres-toggle");
export const libraryGenresSection = document.getElementById("library-genres");
export const libraryTabs = document.getElementById("library-tabs");
export const libraryTodoCount = document.getElementById("library-todo-count");
export const libraryKind = document.getElementById("library-kind");
export const librarySort = document.getElementById("library-sort");
export const libraryLayout = document.getElementById("library-layout");
export const librarySelect = document.getElementById("library-select");
export const libraryShow = document.getElementById("library-show");
export const librarySuggest = document.getElementById("library-suggest");
export const libraryDrawer = document.getElementById("library-drawer");
export const libraryBar = document.getElementById("library-bar");
export const libraryDialog = document.getElementById("library-dialog");
export const libraryStatus = document.getElementById("library-status");

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

export function remember(key, value) {
  try { localStorage.setItem(`library.${key}`, value); } catch { /* fine */ }
}

export const viewing = {
  tab: remembered("tab", "albums", ["albums", "artists", "todo"]),
  kind: remembered("kind", "all", ["all", "album", "single"]),
  sort: remembered("sort", "recent", ["recent", "artist", "album", "year", "plays"]),
  layout: remembered("layout", "grid", ["grid", "list"]),
  // Needs review, no MusicBrainz match, no ReplayGain - or none of them.
  show: "all",
  // The artist whose page is open, on the Artists tab.
  artist: null,
};

export function albumKey(album) {
  return `${album.library_id}/${album.folder}`;
}

export function albumName(album) {
  return [album.artist, album.album].filter(Boolean).join(" - ")
    || album.folder || "this album";
}

export function plural(n, word) {
  return `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
}

export function actionButton(label, className, onClick) {
  const b = el("button", className, label);
  b.type = "button";
  b.addEventListener("click", () => onClick(b));
  return b;
}

export function editField(label, value, extra = {}) {
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

/* --- cover art ----------------------------------------------------------- */

// When each album's cover was last replaced here. The art URL is keyed on a
// track id, which does not change when the picture does, and it is cached
// for a week - so without a stamp the old cover stays on screen. Only for
// this page's life: the listing's own art_version is what survives a reload.
export const artStamps = new Map();

// How long Navidrome takes to notice a changed file and serve its new art.
// A stamp taken sooner is fetched while Navidrome still has the old picture,
// and that answer is what the browser then keeps for a week.
export const RESCAN_WAIT_MS = 20000;

// Navidrome resizes, and the embedded images behind these are often a
// megabyte each. Lazy, so scrolling past two thousand albums does not fetch
// two thousand covers, and removed if there is none rather than leaving a
// broken-image glyph.
export function libraryArt(trackId, size, stamp, className = "") {
  const box = el("div", `art ${className}`.trim());
  if (!trackId) return box;
  const img = el("img");
  img.loading = "lazy";
  img.decoding = "async";
  img.alt = "";
  img.src = `/api/library/art?id=${encodeURIComponent(trackId)}&size=${size}`
    + (stamp ? `&v=${encodeURIComponent(stamp)}` : "");
  img.addEventListener("error", () => img.remove());
  box.append(img);
  return box;
}

export function albumArt(album, size, className) {
  const version = [album.art_version, artStamps.get(albumKey(album))]
    .filter(Boolean).join(".");
  return libraryArt(album.art_id, size, version, className);
}

/* --- selection ----------------------------------------------------------- */

// Kept across searches, tabs and pages, so a record's singles can be found
// one search at a time and combined at the end.
export const selection = {
  on: false,
  albums: new Map(),   // albumKey -> album
  tracks: new Map(),   // track path -> { track, album }
};

export function isPicked(album) {
  return selection.albums.has(albumKey(album));
}

export function looseTracks() {
  return [...selection.tracks.values()]
    .filter((t) => !selection.albums.has(albumKey(t.album)));
}

// What more than one of the library's modules reads and replaces. On one
// object because a module cannot assign a binding it imported.
export const libraryState = {
  // The attention list, kept between visits to the tab and dropped whenever
  // something changes the library.
  attention: null,
  // The cover survey's answer, and the albums it found barred by albumKey -
  // so a card drawn before the survey answered can still be flagged when it
  // does.
  coverSurvey: null,
  barredKeys: new Set(),
  // Every artist, for the Artists tab; dropped when a change can move one.
  artistsCache: null,
  // Which load of the list is the latest, whichever tab it is for. Each
  // loader takes the next number and draws only if nothing has taken one
  // since: a slow albums answer used to land under the Artists tab, and the
  // reverse.
  load: 0,
  // Bumped by forgetCoverSurvey(), so a survey in flight can tell it is
  // out of date.
  surveyGeneration: 0,
};

export function isBarred(album) {
  return album.barred || libraryState.barredKeys.has(albumKey(album));
}

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

// The survey, read once and kept until something changes a cover: the list
// flags barred covers from it and Needs attention lists them. Both used to
// fetch it and fill in the barred set themselves.
export async function loadCoverSurvey() {
  if (!libraryState.coverSurvey) {
    const asked = libraryState.surveyGeneration;
    const survey = await fetchCoverSurvey();
    // Kept only if nothing changed a cover while it was being read. A
    // survey started before a cover was squared answered after it, and was
    // stored over the invalidation: the Cover flag came back on the album
    // just fixed, until the next change.
    if (asked !== libraryState.surveyGeneration) return survey;
    libraryState.coverSurvey = survey;
    libraryState.barredKeys = new Set(survey.albums.map(albumKey));
  }
  return libraryState.coverSurvey;
}

// After any change to a cover: the survey is read again when next wanted,
// and one already on its way is not kept.
export function forgetCoverSurvey() {
  libraryState.coverSurvey = null;
  libraryState.surveyGeneration += 1;
}

// A running operation's progress line. `ordinal` counts the album being
// worked on ("1 of 3") rather than those finished ("0 of 3"); `stopUrl`
// adds a Stop button, for an operation that can stop between albums.
export function showProgress(operation, { label, stopUrl = null, ordinal = false }) {
  if (operation.status !== "running") {
    libraryProgress.replaceChildren();
    libraryProgress.hidden = true;
    return;
  }
  const p = operation.progress;
  const text = p ? `${label}: ${p.done + (ordinal ? 1 : 0)} of ${p.total} — ${p.album}`
    : `${label}: starting…`;
  const nodes = [el("span", "banner-text", text)];
  if (stopUrl) {
    const stop = el("button", "ghost", operation.stopping ? "Stopping…" : "Stop");
    stop.type = "button";
    stop.disabled = !!operation.stopping;
    stop.addEventListener("click", async () => {
      stop.disabled = true;
      stop.textContent = "Stopping…";
      const response = await apiFetch(stopUrl, { method: "POST" })
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
