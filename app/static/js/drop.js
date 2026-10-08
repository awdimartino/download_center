"use strict";

/* --- drop ------------------------------------------------------------------
   The third way music arrives, beside a download and a folder dragged onto
   the network share: picked or dropped here. Everything below only has to
   get the bytes to /api/inbox/upload - the filing is the inbox's, the same
   as it is for the other two roads in. */

import { apiFetch, el, libraryPicker, showError, targetLibrary } from "./core.js";

const dropZone = document.getElementById("drop-zone");
const dropInput = document.getElementById("drop-input");
const dropBatches = document.getElementById("drop-batches");
const dropEmpty = document.getElementById("drop-empty");
libraryPicker(document.getElementById("drop-library"));

// Mirrors uuidtags.AUDIO_SUFFIXES and filer.COVER_SUFFIXES. The server is
// the authority on what it will file; this only skips a request that would
// obviously be refused.
const DROP_AUDIO_EXT = new Set([
  ".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".oga", ".opus",
  ".wav", ".wv", ".aiff", ".ape",
]);
const DROP_COVER_EXT = new Set([".jpg", ".jpeg", ".png", ".webp"]);

// By extension as well as type: "audio/*" alone hid covers, and on some
// systems .ape and .wv, which have no registered audio type there.
dropInput.accept = ["audio/*", ...DROP_AUDIO_EXT, ...DROP_COVER_EXT].join(",");

function dropExt(name) {
  const dot = name.lastIndexOf(".");
  return dot === -1 ? "" : name.slice(dot).toLowerCase();
}

function dropUploadable(relpath) {
  const name = relpath.split("/").pop();
  // A dotfile - macOS's ._ companions above all - is refused by the server.
  // Two or more dots are a title ("...", "... (Continued)"), which it takes.
  if (name.startsWith(".") && !name.startsWith("..")) return false;
  const ext = dropExt(name);
  return DROP_AUDIO_EXT.has(ext) || DROP_COVER_EXT.has(ext);
}

async function dropReadEntries(reader) {
  const out = [];
  for (;;) {
    const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
    if (!batch.length) return out;
    out.push(...batch);
  }
}

// A dragged folder arrives as a tree of entries, not a flat file list -
// walked here so an album's cover ends up beside its tracks under the same
// relative path, which is what lets the server keep one drop's cover art
// from being offered to a different drop's tracks (see inbox.upload_root).
async function dropFilesFromEntry(entry, prefix) {
  if (entry.isFile) {
    const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
    return [[`${prefix}${file.name}`, file]];
  }
  if (entry.isDirectory) {
    const entries = await dropReadEntries(entry.createReader());
    const nested = await Promise.all(
      entries.map((child) => dropFilesFromEntry(child, `${prefix}${entry.name}/`)));
    return nested.flat();
  }
  return [];
}

async function dropFilesFromTransfer(transfer) {
  const items = transfer.items ? [...transfer.items] : [];
  const entries = items
    .map((item) => item.webkitGetAsEntry && item.webkitGetAsEntry())
    .filter(Boolean);
  if (entries.length) {
    const nested = await Promise.all(
      entries.map((entry) => dropFilesFromEntry(entry, "")));
    return nested.flat();
  }
  // No entry API (or nothing recognised as one) - the flat file list is all
  // there is, which is what a picker gives on every browser anyway.
  return [...transfer.files].map((file) => [file.name, file]);
}

function dropBuildBatch(count) {
  const card = el("div", "job");
  const head = el("div", "job-head");
  const title = el("span", "job-title", `${count} file${count === 1 ? "" : "s"}`);
  const meta = el("span", "job-meta", "uploading");
  head.append(title, meta);
  const tracks = el("div", "tracks");
  head.addEventListener("click", () => { tracks.hidden = !tracks.hidden; });
  const error = el("div", "job-error", "");
  error.hidden = true;
  card.append(head, tracks, error);
  return { node: card, title, meta, tracks, error };
}

function dropBuildRow(name) {
  const row = el("div", "track");
  const label = el("span", "track-name", name);
  const status = el("span", "track-status", "waiting");
  const bar = el("div", "bar");
  const fill = el("div", "bar-fill");
  bar.append(fill);
  row.append(label, status, bar);
  return { row, label, status, fill };
}

// How long an upload may go without moving before it is given up on, so a
// connection that has died does not hold every drop queued behind it.
const UPLOAD_STALL_MS = 120000;

function dropUpload(file, relpath, batch, library, onProgress) {
  return new Promise((resolve, reject) => {
    const body = new FormData();
    body.append("file", file, file.name);
    body.append("relpath", relpath);
    if (batch) body.append("batch", batch);
    if (library !== null) body.append("library_id", String(library));
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/inbox/upload");
    // Every way this can end settles the promise. It used to be possible
    // for none to: a 2xx answer that was not JSON threw inside onload, and
    // an upload that simply stopped moving never finished at all. Either
    // way the promise never settled and every later drop queued behind it
    // for good.
    let quiet = null;
    const stalled = () => {
      clearTimeout(quiet);
      quiet = setTimeout(() => xhr.abort(), UPLOAD_STALL_MS);
    };
    xhr.upload.addEventListener("progress", (event) => {
      stalled();
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    });
    xhr.onload = () => {
      clearTimeout(quiet);
      let answer = null;
      try { answer = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
      if (xhr.status >= 200 && xhr.status < 300) {
        if (answer) resolve(answer);
        else reject(new Error("The server's answer could not be read."));
        return;
      }
      reject(new Error((answer && answer.detail) || `Upload failed (${xhr.status})`));
    };
    xhr.onerror = () => { clearTimeout(quiet); reject(new Error("Could not reach the server.")); };
    xhr.onabort = () => {
      clearTimeout(quiet);
      reject(new Error(`Nothing moved for ${UPLOAD_STALL_MS / 1000} seconds; gave up.`));
    };
    xhr.send(body);
    stalled();
  });
}

// Names the drop, so the server files all of it at once now that every file
// has arrived - not track by track while the cover is still uploading.
async function dropFinish(batch, library) {
  const response = await apiFetch(
    `/api/inbox/upload/finish?batch=${encodeURIComponent(batch)}`
      + (library !== null ? `&library_id=${library}` : ""),
    { method: "POST" });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || "Could not finish filing.");
  }
  return response.json();
}

// One drop or pick at a time, in the order they arrived. The Pi's link is
// the bottleneck either way, so running two batches at once would only have
// them contend for it rather than finish any sooner.
let dropChain = Promise.resolve();

function dropQueueBatch(pairs) {
  const items = pairs.filter(([relpath]) => dropUploadable(relpath));
  const skipped = pairs.length - items.length;
  if (!items.length) {
    if (skipped) showError(`Nothing uploadable there - ${skipped} file(s) skipped.`);
    return;
  }

  const card = dropBuildBatch(items.length);
  dropEmpty.hidden = true;
  dropBatches.prepend(card.node);
  const rows = items.map(([relpath]) => {
    const built = dropBuildRow(relpath);
    card.tracks.append(built.row);
    return built;
  });

  // Which library this drop is for, decided when it was dropped. Read per
  // file instead, switching the picker for the next drop while this one
  // was still uploading sent the rest of it, and its finish, to the other
  // library - and the files already sent waited for the poller.
  const library = targetLibrary();
  dropChain = dropChain.then(() => dropRunBatch(items, rows, card, skipped, library));
}

async function dropRunBatch(items, rows, card, skipped, library) {
  let batch = null;
  let failed = 0;
  for (let i = 0; i < items.length; i++) {
    const [relpath, file] = items[i];
    const parts = rows[i];
    parts.row.className = "track uploading";
    parts.status.textContent = "uploading";
    parts.status.className = "track-status uploading";
    try {
      const result = await dropUpload(file, relpath, batch, library, (fraction) => {
        parts.fill.style.width = `${Math.round(fraction * 100)}%`;
      });
      batch = result.batch;
      parts.row.className = "track uploaded";
      parts.status.textContent = "uploaded";
      parts.status.className = "track-status uploaded";
    } catch (err) {
      failed += 1;
      parts.row.className = "track failed";
      parts.status.textContent = "failed";
      parts.status.className = "track-status failed";
      parts.label.title = err.message || "Upload failed";
    }
  }

  const skippedNote = skipped
    ? `${skipped} file(s) skipped - not something this can file.` : "";

  if (!batch) {
    card.meta.textContent = "failed";
    if (skippedNote) { card.error.textContent = skippedNote; card.error.hidden = false; }
    return;
  }

  card.meta.textContent = "filing…";
  try {
    const summary = await dropFinish(batch, library);
    const parts = [];
    if (summary.filed.length) parts.push(`${summary.filed.length} filed`);
    if (failed) parts.push(`${failed} upload${failed === 1 ? "" : "s"} failed`);
    if (summary.waiting) parts.push(`${summary.waiting} still settling`);
    card.meta.textContent = parts.join(" · ") || "filed";
    const notes = [...summary.failures];
    if (skippedNote) notes.push(skippedNote);
    if (notes.length) {
      card.error.textContent = notes.join(" · ");
      card.error.hidden = false;
    }
  } catch (err) {
    card.meta.textContent = "could not finish filing";
    card.error.textContent = [err.message, skippedNote].filter(Boolean).join(" · ");
    card.error.hidden = false;
  }
}

dropZone.addEventListener("click", () => dropInput.click());
["dragenter", "dragover"].forEach((type) => {
  dropZone.addEventListener(type, (event) => {
    event.preventDefault();
    dropZone.classList.add("drag-over");
  });
});
["dragleave", "dragend"].forEach((type) => {
  dropZone.addEventListener(type, () => dropZone.classList.remove("drag-over"));
});
dropZone.addEventListener("drop", async (event) => {
  event.preventDefault();
  dropZone.classList.remove("drag-over");
  dropQueueBatch(await dropFilesFromTransfer(event.dataTransfer));
});
dropInput.addEventListener("change", () => {
  const pairs = [...dropInput.files].map((file) => [file.name, file]);
  dropInput.value = "";
  dropQueueBatch(pairs);
});
