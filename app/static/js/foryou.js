"use strict";

/* --- for you ---------------------------------------------------------------
   What the Download tab shows before anything is searched: shelves of
   things to download next, picked from what you play (app/recommend.py).

   The cards are the search results' own (browse.js), so a recommendation
   is downloaded, opened and shown as queued exactly as a result is. What
   this adds is the shelves, the line saying why each is there, and ✕ for
   "not interested" - with an undo, and an offer to hide the whole artist,
   which is more often what was meant. */

import { apiFetch, el, getJSON, postJSON, showError } from "./core.js";
import { shelfAlbum, shelfArtist, shelfPainters, shelfTrack } from "./browse.js";

const shelvesEl = document.getElementById("fy-shelves");
const whenEl = document.getElementById("fy-when");
const refreshEl = document.getElementById("fy-refresh");
const toastEl = document.getElementById("fy-toast");

// The pass is a day old at most and the stored copy is filtered on every
// read, so coming back to the tab within a few minutes need not ask again.
const FRESH_MS = 5 * 60 * 1000;
const POLL_MS = 5000;

let loadedAt = 0;
let polling = null;
let loadToken = 0;

/* --- words ----------------------------------------------------------------- */

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// "12 Sep" from "2026-09-12", for a release this season.
function released(date) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date || "");
  return m ? `${Number(m[3])} ${MONTHS[Number(m[2]) - 1]}` : (date || "").slice(0, 4);
}

function ago(stamp) {
  const minutes = Math.round((Date.now() - Date.parse(stamp)) / 60000);
  if (!Number.isFinite(minutes)) return "";
  if (minutes < 2) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  return hours < 24 ? `${hours} h ago` : "yesterday";
}

// "Radiohead", "Radiohead and Björk", "Radiohead, Björk and Portishead".
function names(list) {
  if (list.length < 2) return list[0] || "";
  return `${list.slice(0, -1).join(", ")} and ${list[list.length - 1]}`;
}

/* --- not interested -------------------------------------------------------- */

let toastTimer = null;

function toast(text, actions) {
  clearTimeout(toastTimer);
  toastEl.replaceChildren(el("span", "fy-toast-text", text),
    ...actions.map(([label, handler]) => {
      const button = el("button", "ghost", label);
      button.type = "button";
      button.addEventListener("click", () => {
        hideToast();
        handler();
      });
      return button;
    }));
  toastEl.hidden = false;
  toastTimer = setTimeout(hideToast, 8000);
}

function hideToast() {
  clearTimeout(toastTimer);
  toastEl.hidden = true;
}

async function send(kind, key, label, undo = false) {
  try {
    await postJSON("/api/recommendations/dismiss", { kind, key, label, undo });
    return true;
  } catch (error) {
    showError(`Could not save that: ${error.message}`);
    return false;
  }
}

// Off the page now, saved behind it. An artist takes every card of theirs
// with them, on every shelf.
function removeCards(match) {
  shelvesEl.querySelectorAll(".fy-item").forEach((item) => {
    if (match(item)) item.remove();
  });
  shelvesEl.querySelectorAll(".fy-section").forEach((section) => {
    if (!section.querySelector(".fy-item")) section.remove();
  });
  if (!shelvesEl.querySelector(".fy-item")) showNothing();
}

async function dismiss(item) {
  const { kind, key, artistKey, label, artist } = item.dataset;
  removeCards((other) => other === item);
  if (!(await send(kind, key, label))) return load({ force: true });
  const actions = [["Undo", async () => {
    await send(kind, key, label, true);
    load({ force: true });
  }]];
  if (kind !== "artist" && artistKey && artist) {
    actions.push([`Hide all by ${artist}`, () => dismissArtist(artistKey, artist)]);
  }
  toast(`Hidden “${label}”.`, actions);
}

async function dismissArtist(key, name) {
  removeCards((item) => item.dataset.artistKey === key);
  if (!(await send("artist", key, name))) return load({ force: true });
  toast(`Hidden everything by ${name}.`, [["Undo", async () => {
    await send("artist", key, name, true);
    load({ force: true });
  }]]);
}

// The card, with its ✕ and what that ✕ sends.
function item(node, kind, card, label, artist) {
  const box = el("div", `fy-item fy-${kind}`);
  Object.assign(box.dataset, {
    kind, key: card.dismiss_key, artistKey: card.artist_key || "",
    label, artist: artist || "",
  });
  const close = el("button", "fy-dismiss", "✕");
  close.type = "button";
  close.title = "Not interested";
  close.setAttribute("aria-label", `Not interested: ${label}`);
  close.addEventListener("click", (event) => {
    event.stopPropagation();
    dismiss(box);
  });
  box.append(node, close);
  return box;
}

/* --- shelves --------------------------------------------------------------- */

// A row of covers that scrolls sideways, with arrows for a mouse.
function shelf(title, note, items, className = "fy-shelf") {
  const head = el("h3", "lib-section-title fy-title", title);
  if (note) head.append(el("small", "", note));
  const row = el("div", className);
  row.append(...items);
  const section = el("section", "fy-section");
  if (className === "fy-shelf") {
    const arrows = el("span", "fy-arrows");
    for (const [label, sign] of [["‹", -1], ["›", 1]]) {
      const button = el("button", "ghost fy-arrow", label);
      button.type = "button";
      button.setAttribute("aria-label", sign < 0 ? "Scroll back" : "Scroll on");
      button.addEventListener("click", () =>
        row.scrollBy({ left: sign * row.clientWidth * 0.85, behavior: "smooth" }));
      arrows.append(button);
    }
    head.append(arrows);
  }
  section.append(head, row);
  return section;
}

function albumItem(card, sub) {
  return item(shelfAlbum(card, sub), "album", card, card.name, card.primary_artist || card.artist);
}

function render(data) {
  shelfPainters.length = 0;
  const { shelves } = data;
  const parts = [];

  if (shelves.new.length) {
    parts.push(shelf("New from your artists", "Last 90 days",
      shelves.new.map((a) => albumItem(a, [a.artist, released(a.released)].join(" · ")))));
  }
  if (shelves.missing.length) {
    const who = [...new Set(shelves.missing.map((a) => a.primary_artist || a.artist))];
    parts.push(shelf("Missing from your artists",
      who.length > 3 ? `Albums by ${who.slice(0, 2).join(", ")} and others you don't have`
        : `Albums by ${names(who)} you don't have`,
      shelves.missing.map((a) => albumItem(a))));
  }
  if (shelves.similar.length) {
    parts.push(shelf("Artists like yours", "Not in your library yet",
      shelves.similar.map((artist) => item(
        shelfArtist(artist, `Like ${names(artist.because.slice(0, 2))}`),
        "artist", artist, artist.name, artist.name))));
  }
  if (shelves.because.length) {
    const grid = el("div", "fy-because");
    for (const { seed, tracks } of shelves.because) {
      const box = el("section", "fy-section fy-songs");
      const head = el("h3", "lib-section-title fy-title");
      head.append(`Because you played “${seed.title}”`, el("small", "", seed.artist));
      const list = el("div", "br-songs");
      list.append(...tracks.map((t) =>
        item(shelfTrack(t), "track", t, t.name, t.primary_artist || t.artist)));
      box.append(head, list);
      grid.append(box);
    }
    parts.push(grid);
  }
  for (const { genre, albums } of shelves.genres) {
    parts.push(shelf(`More ${genre}`, "Well loved on Last.fm, not in your library",
      albums.map((a) => albumItem(a))));
  }

  const notes = [...data.problems];
  if (!data.lastfm) {
    notes.push("Similar artists, songs and genre picks need a Last.fm API key: "
               + "set lastfm_api_key in config.toml.");
  }
  if (notes.length) {
    const box = el("div", "fy-notes");
    box.append(...notes.map((text) => el("p", "", text)));
    parts.push(box);
  }

  shelvesEl.replaceChildren(...parts);
  if (!shelvesEl.querySelector(".fy-item")) showNothing(notes);
}

function showNothing(notes = []) {
  const box = el("p", "empty fy-empty",
    "Nothing to suggest yet. These are picked from what you play and star, "
    + "so they arrive with a little listening.");
  const kept = notes.length ? [...shelvesEl.querySelectorAll(".fy-notes")] : [];
  shelvesEl.replaceChildren(box, ...kept);
}

function showWaiting(text) {
  shelfPainters.length = 0;
  const box = el("div", "fy-waiting");
  box.append(el("span", "fy-spinner"), el("span", "", text));
  shelvesEl.replaceChildren(box);
}

function showFailed(reason) {
  shelfPainters.length = 0;
  const box = el("div", "fy-failed");
  box.append(el("p", "empty", `No recommendations: ${reason}`));
  shelvesEl.replaceChildren(box);
}

/* --- loading ---------------------------------------------------------------- */

function setWhen(data) {
  if (data.status !== "ready") {
    whenEl.textContent = "";
    refreshEl.hidden = data.status !== "failed";
    refreshEl.textContent = "Try again";
    return;
  }
  whenEl.textContent = data.refreshing ? "Updating…"
    : data.computed_at ? `Updated ${ago(data.computed_at)}` : "";
  refreshEl.hidden = data.refreshing;
  refreshEl.textContent = "Refresh";
}

function poll(again) {
  clearTimeout(polling);
  polling = again ? setTimeout(() => load({ force: true, quiet: true }), POLL_MS) : null;
}

// `quiet`: a poll, which leaves the shelves alone unless the pass behind
// them has changed - rebuilding them under somebody's pointer every five
// seconds would drop their hover and their scroll.
let shownPass = null;

async function load({ force = false, quiet = false } = {}) {
  if (!force && Date.now() - loadedAt < FRESH_MS) return;
  const token = ++loadToken;
  loadedAt = Date.now();
  if (!quiet && !shelvesEl.childElementCount) showWaiting("Looking at what you play…");
  let data;
  try {
    data = await getJSON("/api/recommendations");
  } catch (error) {
    if (token !== loadToken) return;
    loadedAt = 0;
    setWhen({ status: "failed" });
    showFailed(error.message);
    return;
  }
  if (token !== loadToken) return;
  setWhen(data);
  if (data.status === "computing") {
    showWaiting("Working out what to suggest from what you play. "
                + "The first time takes a minute or so.");
  } else if (data.status === "failed") {
    loadedAt = 0;
    showFailed(data.reason || "something went wrong.");
  } else if (!quiet || data.computed_at !== shownPass) {
    shownPass = data.computed_at;
    render(data);
  }
  poll(data.status === "computing" || data.refreshing);
}

refreshEl.addEventListener("click", async () => {
  refreshEl.hidden = true;
  whenEl.textContent = "Updating…";
  try {
    await apiFetch("/api/recommendations/refresh", { method: "POST" });
  } catch { /* the load below says what is wrong */ }
  load({ force: true, quiet: shelvesEl.querySelector(".fy-item") !== null });
});

// Called by main.js each time the Download tab is shown.
export function loadForYou() {
  load();
}
