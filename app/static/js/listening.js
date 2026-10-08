"use strict";

/* --- listening -------------------------------------------------------------
   Home's listening detail: the play-count readings and imported Last.fm
   history, broken down by range. The first thing it shows is what has been
   captured - because the number that matters most is still "is this
   collecting or not". */

import { apiFetch, el, setBanner } from "./core.js";
import { stat, hourlyBars, albumBars, genreBars, sessionStats } from "./charts.js";

const listeningEl = document.getElementById("listening");
const listeningEmpty = document.getElementById("listening-empty");
const listeningError = document.getElementById("listening-error");
const listeningCoverage = document.getElementById("listening-coverage");
const listeningAlbums = document.getElementById("listening-albums");
const listeningGenres = document.getElementById("listening-genres");
const listeningHourly = document.getElementById("listening-hourly");
const listeningSession = document.getElementById("listening-session");
const listeningMore = document.getElementById("listening-more");
const sessionPart = document.getElementById("listening-session-part");
const hourlyPart = document.getElementById("listening-hourly-part");
const albumsPart = document.getElementById("listening-albums-part");
const genresPart = document.getElementById("listening-genres-part");
// The default range. overview.OPENING_DAYS warms the cache for the same
// one; the markup carries no default of its own (markShown sets it).
let listeningDays = 3650;
// Set when a date range is chosen; takes precedence over listeningDays.
let listeningDates = null;
const listenCustom = document.getElementById("listen-custom");
const listenDates = document.getElementById("listen-dates");
const listenFrom = document.getElementById("listen-from");
const listenTo = document.getElementById("listen-to");

// The first few rows say what the list is; the rest are one tap away rather
// than fifty rows on a page meant to be read at a glance.
const TRACKS_SHOWN = 10;
let showAllTracks = false;
let lastTracks = [];

// A part with nothing in it for this range is hidden whole, heading and all.
function showPart(part, hasData) {
  part.hidden = !hasData;
}

function renderTracks() {
  const most = lastTracks.length ? lastTracks[0].plays : 1;
  const shown = showAllTracks ? lastTracks : lastTracks.slice(0, TRACKS_SHOWN);
  listeningEl.replaceChildren(
    ...shown.map((track, index) => listeningRow(track, index + 1, most)));
  listeningMore.hidden = lastTracks.length <= TRACKS_SHOWN;
  listeningMore.textContent = showAllTracks
    ? "Show fewer"
    : `Show all ${lastTracks.length} tracks`;
  listeningMore.setAttribute("aria-expanded", String(showAllTracks));
}

function coverage(cover, window) {
  const boxes = [stat(
    "Plays in view", window.plays.toLocaleString(),
    `${window.start} to ${window.end}`)];

  if (cover.imported_plays) {
    boxes.push(stat(
      "Imported history", cover.imported_plays.toLocaleString(),
      `${(cover.imported_sources || []).join(", ")}, from ${cover.imported_from}`));
  }
  boxes.push(stat(
    "Days collecting", (cover.days_run || 0).toLocaleString(),
    cover.last_run ? `most recently ${cover.last_run}` : "none yet"));
  // The one that is a health question rather than a statistic: a collector
  // that quietly stopped looks exactly like one that found nothing.
  boxes.push(stat(
    "Collecting", cover.up_to_date ? "yes" : "no",
    cover.last_reading
      ? `last read ${cover.last_reading.replace("T", " ")}`
      : "never read"));

  // Plays the collector is dropping, because the track has no UUID to
  // follow. Said here, where the missing plays would be noticed.
  if (cover.without_uuid) {
    boxes.push(stat(
      "Not being recorded", cover.without_uuid.toLocaleString(),
      "played tracks with no UUID; Health lists them"));
  }

  listeningCoverage.replaceChildren(...boxes);
}

function listeningRow(track, rank, most) {
  const row = el("div", `listen-row${track.known ? "" : " gone"}`);
  const bar = el("div", "listen-bar");
  const fill = el("div", "listen-bar-fill");
  // Proportional to the top row, so the shape of the list is readable
  // without reading any of the numbers.
  fill.style.width = `${Math.max(2, Math.round(100 * track.plays / most))}%`;
  bar.append(fill);
  row.append(
    el("span", "listen-rank", String(rank)),
    el("span", "listen-title", track.title),
    el("span", "listen-artist", track.artist || ""),
    bar,
    el("span", "listen-plays", track.plays.toLocaleString())
  );
  if (!track.known) {
    row.title = "Played, but no longer in the library. The count is kept "
              + "against the track's identity, not its file.";
  }
  return row;
}

// Which load is the latest. Each range click starts a request, and a slow
// one (Year) used to arrive after a fast cached one (All time) and be drawn
// under it. Only the newest answer is drawn.
let listeningRequest = 0;

export async function loadListening() {
  const mine = ++listeningRequest;
  try {
    const query = listeningDates
      ? `start=${listeningDates.start}&end=${listeningDates.end}`
      : `days=${listeningDays}`;
    const data = await apiFetch(
      `/api/playcounts/top?${query}&limit=50`
    ).then((r) => r.json());
    if (mine !== listeningRequest) return;
    if (data.detail) {
      setBanner(listeningError, data.detail, "warn");
      return;
    }
    setBanner(listeningError, "");
    const tracks = data.tracks || [];
    coverage(data.coverage || {}, data);

    lastTracks = tracks;
    renderTracks();
    listeningEmpty.textContent = tracks.length
      ? "" : "Nothing played in this window yet.";
    listeningEmpty.hidden = tracks.length > 0;

    const session = data.longest_session || {};
    const hourly = data.hourly || [];
    const albums = data.albums || [];
    const genres = data.genres || [];
    showPart(sessionPart, Boolean(session.count));
    showPart(hourlyPart, hourly.some((h) => h.plays > 0));
    showPart(albumsPart, albums.length > 0);
    showPart(genresPart, genres.length > 0);
    listeningSession.replaceChildren(...sessionStats(session));
    listeningHourly.replaceChildren(hourlyBars(hourly));
    listeningAlbums.replaceChildren(...albumBars(albums));
    listeningGenres.replaceChildren(...genreBars(genres));
  } catch (err) {
    if (mine !== listeningRequest) return;
    listeningEmpty.hidden = false;
    listeningEmpty.textContent = `Could not read play counts: ${err.message}`;
  }
}

// A chosen range of dates, both ends inclusive, as if picked under Dates.
// The "In <year>" home tile jumps here with its calendar year: the Year
// button is the last 365 days, which is not the number the tile shows.
export function selectDates(start, end) {
  listeningDates = { start, end };
  listenFrom.value = start;
  listenTo.value = end;
  markShown();
  listenDates.hidden = true;
  listenCustom.setAttribute("aria-expanded", "false");
  loadListening();
}

export function selectRange(days) {
  listeningDays = days;
  listeningDates = null;
  markShown();
  listenDates.hidden = true;
  listenCustom.setAttribute("aria-expanded", "false");
  loadListening();
}

// One range button marked as selected: the class for the eye, aria-pressed
// for a screen reader. Dates used to change neither until its range was
// submitted, so while choosing, the old range still looked selected.
function markRange(active) {
  document.querySelectorAll(".listen-range .range").forEach((button) => {
    const on = button === active;
    button.classList.toggle("active", on);
    button.setAttribute("aria-pressed", String(on));
  });
}

// Mark whichever range is actually on screen.
function markShown() {
  markRange(listeningDates ? listenCustom
    : document.querySelector(`.listen-range .range[data-days="${listeningDays}"]`));
}

// Local date as YYYY-MM-DD, the format a date input and the server both use.
function isoDay(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

// Opens the date pickers, pre-filled with the last 30 days so the first
// "Show" already does something sensible.
listenCustom.addEventListener("click", () => {
  const open = listenDates.hidden;
  listenDates.hidden = !open;
  listenCustom.setAttribute("aria-expanded", String(open));
  // Open: Dates is what is being chosen. Closed without a submit: back to
  // the range still on screen.
  if (open) markRange(listenCustom);
  else markShown();
  if (open && !listenFrom.value) {
    const today = new Date();
    const monthAgo = new Date(today);
    monthAgo.setDate(today.getDate() - 29);
    listenFrom.value = isoDay(monthAgo);
    listenTo.value = isoDay(today);
  }
  if (open) listenFrom.focus();
});

listenDates.addEventListener("submit", (event) => {
  event.preventDefault();
  if (listenFrom.value > listenTo.value) {
    setBanner(listeningError, "The start date is after the end date.", "warn");
    return;
  }
  listeningDates = { start: listenFrom.value, end: listenTo.value };
  markShown();
  loadListening();
});

document.querySelectorAll(".listen-range .range[data-days]").forEach((button) => {
  button.addEventListener("click", () => selectRange(Number(button.dataset.days)));
});

listeningMore.addEventListener("click", () => {
  showAllTracks = !showAllTracks;
  renderTracks();
});

markShown();
