"use strict";

/* --- quarantine ----------------------------------------------------------------
   Everything set aside from the library: tracks removed by hand in the
   Library, and the losing copies of duplicates. An album to a card, newest
   first, each track with Restore and Delete for good (app/quarantine.py).

   Restore puts a track back where it was, which is how Navidrome knows it,
   so its stars and plays return with it. A track that lost to a duplicate
   is a duplicate again once restored, and the page says so before it does. */

import { el, getJSON, plural, postJSON, setBanner } from "./core.js";
import { showView } from "./nav.js";

const listEl = document.getElementById("quarantine-list");
const emptyEl = document.getElementById("quarantine-empty");
const summaryEl = document.getElementById("quarantine-summary");
const countEl = document.getElementById("quarantine-count");
const filterEl = document.getElementById("quarantine-filter");
const searchEl = document.getElementById("quarantine-search");
const ageEl = document.getElementById("quarantine-age");
const emptyOldEl = document.getElementById("quarantine-empty-old");
const noteEl = document.getElementById("quarantine-note");

const REASON = {
  removed: ["Removed by hand", ""],
  duplicate: ["Lost to a duplicate", "tone-accent"],
  unknown: ["No record", ""],
};
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

let data = null;
let reason = "all";

function bytes(n) {
  if (!n) return "";
  return n > 1e9 ? `${(n / 1e9).toFixed(1)} GB` : `${Math.max(1, Math.round(n / 1e6))} MB`;
}

function when(stamp) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(stamp || "");
  return m ? `${Number(m[3])} ${MONTHS[Number(m[2]) - 1]} ${m[1]}` : "";
}

function setCount(n) {
  countEl.textContent = n ? String(n) : "";
  countEl.hidden = !n;
}

/* --- loading ---------------------------------------------------------------------- */

export async function loadQuarantine() {
  try {
    data = await getJSON("/api/quarantine");
  } catch (err) {
    listEl.replaceChildren();
    emptyEl.textContent = `Could not read the quarantine: ${err.message}`;
    emptyEl.hidden = false;
    return;
  }
  setCount(data.tracks);
  render();
}

// The menu's quiet count, at start-up, without drawing the page.
export async function loadQuarantineCount() {
  try {
    setCount((await getJSON("/api/quarantine")).tracks);
  } catch { /* the page says why when it is opened */ }
}

/* --- drawing ------------------------------------------------------------------------ */

function matches(track, album, needle) {
  if (reason !== "all" && track.reason !== reason) return false;
  if (!needle) return true;
  return [track.title, track.artist, track.album, track.was, album.album, album.artist]
    .some((text) => (text || "").toLowerCase().includes(needle));
}

function render() {
  if (!data) return;
  const needle = searchEl.value.trim().toLowerCase();
  const shown = data.albums
    .map((album) => ({ ...album, tracks: album.tracks.filter((t) => matches(t, album, needle)) }))
    .filter((album) => album.tracks.length);

  const by = data.by_reason || {};
  const parts = [`${plural(data.tracks, "track")} set aside`];
  if (data.bytes) parts[0] += `, ${bytes(data.bytes)}`;
  for (const [key, [label]] of Object.entries(REASON)) {
    if (by[key]) parts.push(`${by[key]} ${label.toLowerCase()}`);
  }
  summaryEl.textContent = data.tracks ? parts.join(" · ") : "";
  filterEl.hidden = emptyOldEl.parentElement.hidden = searchEl.parentElement.hidden = !data.tracks;

  listEl.replaceChildren(...shown.map(albumCard));
  emptyEl.textContent = !data.tracks ? "Nothing has been set aside."
    : "Nothing set aside matches that.";
  emptyEl.hidden = shown.length > 0;
}

function albumCard(album) {
  const card = el("section", "q-album");
  const name = el("div", "q-album-name");
  name.append(el("strong", "", album.album || album.folder || "Loose tracks"),
              el("small", "", [album.artist, album.library, plural(album.tracks.length, "track"),
                               bytes(album.tracks.reduce((n, t) => n + t.size, 0))]
                .filter(Boolean).join(" · ")));
  const actions = el("div", "q-album-actions");
  if (album.tracks.length > 1) {
    actions.append(
      button("Restore all", "ghost", () => act("restore", album.tracks, card)),
      button("Delete all…", "ghost danger", () => act("delete", album.tracks, card)));
  }
  const head = el("div", "q-album-head");
  head.append(name, actions);
  card.append(head, ...album.tracks.map((track) => trackRow(track, card)));
  return card;
}

function trackRow(track, card) {
  const row = el("div", "q-track");
  const name = el("span", "q-track-name", track.title);
  name.append(el("small", "", track.was));
  const [label, tone] = REASON[track.reason] || REASON.unknown;
  const pill = el("span", `lib-pill ${tone}`.trim(), `${label}${track.moved_at ? ` · ${when(track.moved_at)}` : ""}`);
  const notes = [];
  if (track.decided_by) notes.push(`Set aside by ${track.decided_by}`);
  if (track.kept) notes.push(`Kept instead: ${track.kept}`);
  if (notes.length) pill.title = notes.join("\n");
  const actions = el("span", "q-track-actions");
  actions.append(
    button("Restore", "ghost", () => act("restore", [track], card)),
    button("Delete…", "ghost danger", () => act("delete", [track], card)));
  row.append(name, pill, actions);
  return row;
}

function button(label, className, onClick) {
  const b = el("button", className, label);
  b.type = "button";
  b.addEventListener("click", onClick);
  return b;
}

/* --- acting -------------------------------------------------------------------------- */

async function act(kind, tracks, card) {
  const n = tracks.length;
  if (kind === "delete" && !confirm(
    `Delete ${n === 1 ? `“${tracks[0].title}”` : plural(n, "track")} for good?\n\n`
    + "The file is removed from disk. This cannot be undone.")) return;
  const duplicates = tracks.filter((t) => t.reason === "duplicate").length;
  if (kind === "restore" && duplicates && !confirm(
    `${duplicates === n ? (n === 1 ? "This track" : "These tracks") : plural(duplicates, "of these tracks")} `
    + "lost to a duplicate. Restoring makes a duplicate again, and it will be back on "
    + "the Duplicates page.\n\nRestore anyway?")) return;

  card.classList.add("busy");
  let outcome;
  try {
    outcome = await postJSON(`/api/quarantine/${kind}`, { keys: tracks.map((t) => t.key) });
  } catch (err) {
    card.classList.remove("busy");
    setBanner(noteEl, `Could not ${kind}: ${err.message}`, "warn");
    return;
  }
  const done = (kind === "restore" ? outcome.restored : outcome.deleted).length;
  const failed = outcome.failed || [];
  setBanner(noteEl,
    `${kind === "restore" ? "Restored" : "Deleted"} ${plural(done, "track")}.`
      + (kind === "restore" && done ? " Navidrome has been asked to scan." : "")
      + (failed.length ? ` ${failed.length} failed: ${failed.slice(0, 3).join("; ")}` : ""),
    failed.length ? "warn" : "notice");
  await loadQuarantine();
}

emptyOldEl.addEventListener("click", async () => {
  if (!data) return;
  const days = Number(ageEl.value);
  const cutoff = new Date(Date.now() - days * 864e5).toISOString();
  const old = data.albums.flatMap((a) => a.tracks).filter((t) => t.moved_at && t.moved_at < cutoff);
  if (!old.length) {
    setBanner(noteEl, `Nothing has been set aside for longer than that.`, "notice");
    return;
  }
  if (!confirm(`Delete ${plural(old.length, "track")} set aside more than `
    + `${ageEl.selectedOptions[0].textContent} ago, for good?\n\nThis cannot be undone.`)) return;
  emptyOldEl.disabled = true;
  try {
    const outcome = await postJSON("/api/quarantine/empty", { older_than_days: days });
    setBanner(noteEl, `Deleted ${plural(outcome.deleted.length, "track")}.`
      + (outcome.failed.length ? ` ${outcome.failed.length} failed.` : ""),
      outcome.failed.length ? "warn" : "notice");
  } catch (err) {
    setBanner(noteEl, `Could not delete: ${err.message}`, "warn");
  } finally {
    emptyOldEl.disabled = false;
  }
  await loadQuarantine();
});

filterEl.querySelectorAll("button").forEach((b) => {
  b.addEventListener("click", () => {
    reason = b.dataset.reason;
    filterEl.querySelectorAll("button").forEach((x) =>
      x.setAttribute("aria-pressed", String(x === b)));
    render();
  });
});
filterEl.querySelector('[data-reason="all"]').setAttribute("aria-pressed", "true");
searchEl.addEventListener("input", render);

// Links from other pages ("…waits on the Quarantine page").
document.addEventListener("click", (event) => {
  const link = event.target.closest("[data-goto]");
  if (link) showView(link.dataset.goto);
});
