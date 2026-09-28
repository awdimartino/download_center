"use strict";

/* --- listening -------------------------------------------------------------
   The snapshots have been running since before there was anywhere to read
   them, which made four years of imported history and a nightly job look
   from the outside exactly like nothing happening at all. This is that
   record, and the first thing it shows is what has been captured - because
   the number that matters most is still "is this collecting or not". */

import { el, setBanner } from "./core.js";
import { stat, hourlyBars, albumBars, genreBars, sessionStats } from "./charts.js";

const listeningEl = document.getElementById("listening");
const listeningEmpty = document.getElementById("listening-empty");
const listeningError = document.getElementById("listening-error");
const listeningCoverage = document.getElementById("listening-coverage");
const listeningAlbums = document.getElementById("listening-albums");
const listeningGenres = document.getElementById("listening-genres");
const listeningHourly = document.getElementById("listening-hourly");
const listeningSession = document.getElementById("listening-session");
let listeningDays = 3650;

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

export async function loadListening() {
  try {
    const data = await fetch(
      `/api/playcounts/top?days=${listeningDays}&limit=50`
    ).then((r) => r.json());
    if (data.detail) {
      setBanner(listeningError, data.detail, "warn");
      return;
    }
    setBanner(listeningError, "");
    const tracks = data.tracks || [];
    coverage(data.coverage || {}, data);

    const most = tracks.length ? tracks[0].plays : 1;
    listeningEl.replaceChildren(
      ...tracks.map((track, index) => listeningRow(track, index + 1, most)));
    listeningEmpty.textContent = tracks.length
      ? "" : "Nothing played in this window yet.";
    listeningEmpty.hidden = tracks.length > 0;

    listeningSession.replaceChildren(...sessionStats(data.longest_session || {}));
    listeningHourly.replaceChildren(hourlyBars(data.hourly || []));
    listeningAlbums.replaceChildren(...albumBars(data.albums || []));
    listeningGenres.replaceChildren(...genreBars(data.genres || []));
  } catch (err) {
    listeningEmpty.hidden = false;
    listeningEmpty.textContent = `Could not read play counts: ${err.message}`;
  }
}

// Shared with the "In <year>" home tile, which jumps here already filtered
// to the Year button rather than leaving the visitor to click it themselves.
export function selectRange(days) {
  listeningDays = days;
  document.querySelectorAll(".listen-range .range").forEach((button) => {
    button.classList.toggle("active", Number(button.dataset.days) === days);
  });
  loadListening();
}

document.querySelectorAll(".listen-range .range").forEach((button) => {
  button.addEventListener("click", () => selectRange(Number(button.dataset.days)));
});
