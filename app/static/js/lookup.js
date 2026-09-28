"use strict";

// One track or album, opened by its Navidrome id rather than by browsing to
// it, with a manual quarantine - the same directory and ledger a resolved
// duplicate uses - for the times a file is simply wrong rather than merely
// a worse copy of a right one.

import { el, action, showError } from "./core.js";

const lookupForm = document.getElementById("lookup-form");
const lookupInput = document.getElementById("lookup-id");
const lookupError = document.getElementById("lookup-error");
const lookupResult = document.getElementById("lookup-result");

async function quarantineLookup(id, kind, card) {
  showError("");
  try {
    const response = await fetch("/api/lookup/quarantine", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, type: kind }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      showError(payload.detail || `Request failed (${response.status})`);
      return;
    }
    const moved = payload.quarantined || [];
    const failed = payload.failed || [];
    const parts = [];
    if (moved.length) {
      parts.push(`Set aside ${moved.length} file${moved.length === 1 ? "" : "s"} ` +
                 "to duplicates-removed/.");
    }
    if (failed.length) parts.push(`Could not move: ${failed.join("; ")}`);
    card.append(el("p", failed.length ? "warn" : "notice",
                   parts.join(" ") || "Nothing was moved."));
    card.querySelectorAll("button").forEach((button) => (button.disabled = true));
  } catch {
    showError("Could not reach the server.");
  }
}

function renderLookupTrack(data) {
  const card = el("div", "dupe");
  const head = el("div", "dupe-head");
  head.append(el("span", "dupe-title", `${data.artist} — ${data.title}`));
  if (data.starred) head.append(el("span", "dupe-star", "★"));
  card.append(
    head,
    el("p", "panel-sub", `${data.album || "—"} · ${data.library}`),
    el("p", "dupe-path", data.path)
  );
  card.append(action("Quarantine this track", "primary", () => {
    if (!confirm(
      `Move this file to duplicates-removed/ inside ${data.library}?\n\n` +
      `${data.artist} — ${data.title}\n${data.path}\n\n` +
      "This cannot be undone from here.")) return;
    quarantineLookup(data.id, "track", card);
  }));
  return card;
}

function renderLookupAlbum(data) {
  const card = el("div", "dupe");
  const head = el("div", "dupe-head");
  head.append(el("span", "dupe-title", `${data.artist} — ${data.album}`));
  card.append(
    head,
    el("p", "panel-sub", `${data.library} · ${data.folder}`)
  );
  data.tracks.forEach((track) => {
    card.append(el("p", "panel-sub",
                   `${track.track_no || ""}. ${track.title}`.trim()));
  });
  card.append(action(
    `Quarantine all ${data.tracks.length} track${data.tracks.length === 1 ? "" : "s"}`,
    "primary", () => {
      if (!confirm(
        `Move ${data.tracks.length} file(s) to duplicates-removed/ inside ` +
        `${data.library}?\n\n${data.artist} — ${data.album}\n\n` +
        "This cannot be undone from here.")) return;
      quarantineLookup(data.id, "album", card);
    }));
  return card;
}

async function runLookup(rawId) {
  lookupError.hidden = true;
  lookupResult.replaceChildren();
  const id = rawId.trim();
  if (!id) return;

  try {
    const response = await fetch(`/api/lookup?id=${encodeURIComponent(id)}`);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      lookupError.textContent = data.detail || "No track or album has that id.";
      lookupError.hidden = false;
      return;
    }
    lookupResult.append(
      data.type === "album" ? renderLookupAlbum(data) : renderLookupTrack(data));
  } catch {
    lookupError.textContent = "Could not reach the server.";
    lookupError.hidden = false;
  }
}

lookupForm.addEventListener("submit", (event) => {
  event.preventDefault();
  runLookup(lookupInput.value);
});
