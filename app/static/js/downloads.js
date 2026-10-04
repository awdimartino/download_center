"use strict";

/* --- downloads -------------------------------------------------------------
   The queue, beside the search rather than above it. It used to be a stack
   of cards on top of the results, newest first, finished ones included - so
   every search after a good afternoon's downloading meant scrolling past
   everything that had already worked to reach what you had just asked for.

   Now it is grouped by what it needs from you: what is moving, what failed,
   and what finished. Finished is one folded line, so it can never push
   anything anywhere. On a phone the whole thing folds into a pill at the
   bottom of the screen and opens as a sheet. */

import { el, remoteArt, showError } from "./core.js";
import { jobs, onJobs } from "./ws.js";

const view = document.getElementById("view-browse");
const listEl = document.getElementById("jobs");
const summaryEl = document.getElementById("downloads-summary");
const closeEl = document.getElementById("downloads-close");
const pillEl = document.getElementById("downloads-pill");
const scrimEl = document.getElementById("downloads-scrim");

// An item is moving through these on its way to "complete".
export const MOVING = new Set(["matching", "downloading", "retrying", "tagging", "filing"]);
const SETTLED = new Set(["complete", "failed", "cancelled"]);

const STEP = {
  pending: "Waiting",
  matching: "Finding a match",
  downloading: "Downloading",
  retrying: "Retrying",
  tagging: "Tagging",
  filing: "Filing",
  complete: "Done",
  failed: "Failed",
  cancelled: "Cancelled",
};

// Which shelf a job sits on. "partial" is a failure as far as you are
// concerned: something you asked for did not arrive.
export function jobGroup(job) {
  if (job.status === "complete") return "done";
  if (["failed", "partial", "cancelled"].includes(job.status)) return "attention";
  return "active";
}

export function settledCount(job) {
  return job.items.filter((i) => SETTLED.has(i.status)).length;
}

// 0..1 across the whole job, counting the track in flight by its own bar.
export function jobProgress(job) {
  if (!job.items.length) return 0;
  const moving = job.items.find((i) => i.status === "downloading");
  return (settledCount(job) + (moving ? moving.progress || 0 : 0)) / job.items.length;
}

// What a job is called and looks like before the server has read the link.
// Resolving a big album takes a few seconds, and "https://open.spotify.com/…"
// with an empty square is a poor way to say "the thing you just pressed".
const expected = new Map();   // source url -> { title, cover }

export function expectJob(url, title, cover) {
  expected.set(url, { title, cover });
}

function titleOf(job) {
  return job.title || expected.get(job.source_url)?.title || job.source_url;
}

function coverOf(job) {
  return job.items.find((i) => i.cover_url)?.cover_url
    || expected.get(job.source_url)?.cover || null;
}

function ago(iso) {
  const minutes = Math.round((Date.now() - Date.parse(iso)) / 60000);
  if (!(minutes >= 1)) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  return hours < 24 ? `${hours} h ago` : `${Math.round(hours / 24)} d ago`;
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function describe(job) {
  const n = job.items.length;
  if (job.status === "resolving") return "Reading the link…";
  if (job.status === "complete") {
    const unnamed = job.items.filter((i) => i.warning).length;
    if (unnamed) return `${plural(unnamed, "track")} filed without identity tags`;
    // The title already names the artist; say only what it does not.
    return n === 1 ? `Downloaded ${ago(job.created_at)}` : `${plural(n, "track")} · ${ago(job.created_at)}`;
  }
  if (job.status === "cancelled") return "Cancelled";
  if (job.status === "failed" || job.status === "partial") {
    const failed = job.items.filter((i) => i.status === "failed");
    if (n === 1 || job.status === "failed" && !failed.length) {
      return job.error || failed[0]?.error || "Failed";
    }
    return `${failed.length} of ${n} failed`;
  }
  const current = job.items.find((i) => MOVING.has(i.status));
  if (!current) return "Waiting its turn";
  if (n === 1) return STEP[current.status];
  return `${settledCount(job)} of ${n} · ${current.title}`;
}

/* --- one job ----------------------------------------------------------------
   Kept by id and patched in place. A progress message arrives twice a second
   per running job, and rebuilding the list each time is what used to cost a
   phone its scroll position inside the queue. */

const nodes = new Map();      // job id -> { card, art, title, sub, bar, fill, remove, tracks }
const expanded = new Set();   // job ids whose track list is open

function buildJob(job) {
  const card = el("div", "dl-job");
  const head = el("div", "dl-job-head");
  head.tabIndex = 0;
  head.setAttribute("role", "button");
  const art = el("span", "dl-job-art");
  const text = el("div", "dl-job-text");
  const title = el("span", "dl-job-title");
  const sub = el("span", "dl-job-sub");
  const bar = el("div", "dl-job-bar");
  const fill = el("div", "dl-job-fill");
  bar.append(fill);
  text.append(title, sub, bar);

  const remove = el("button", "dl-job-remove", "✕");
  remove.type = "button";
  remove.addEventListener("click", (event) => {
    event.stopPropagation();
    deleteJob(job.id);
  });
  head.append(art, text, remove);

  const tracks = el("div", "dl-job-tracks");
  const toggle = () => {
    if (expanded.has(job.id)) expanded.delete(job.id);
    else expanded.add(job.id);
    render();
  };
  head.addEventListener("click", toggle);
  head.addEventListener("keydown", (event) => {
    if (event.target !== head || (event.key !== "Enter" && event.key !== " ")) return;
    event.preventDefault();
    toggle();
  });

  card.append(head, tracks);
  const parts = { card, head, art, title, sub, bar, fill, remove, tracks, cover: undefined };
  nodes.set(job.id, parts);
  return parts;
}

function updateJob(job) {
  const parts = nodes.get(job.id) || buildJob(job);
  const group = jobGroup(job);
  const name = titleOf(job);

  const cover = coverOf(job);
  if (cover !== parts.cover) {
    parts.cover = cover;
    parts.art.replaceChildren(remoteArt(cover));
  }
  parts.title.textContent = name;
  parts.title.title = name;
  parts.sub.textContent = describe(job);
  parts.sub.classList.toggle("tone-bad", group === "attention");
  parts.sub.classList.toggle("tone-warn", group === "done" && job.items.some((i) => i.warning));
  parts.bar.hidden = group !== "active";
  parts.fill.style.width = `${Math.round(jobProgress(job) * 100)}%`;
  const label = group === "active" ? `Cancel ${name}` : `Clear ${name}`;
  parts.remove.title = group === "active" ? "Cancel" : "Clear from the list";
  parts.remove.setAttribute("aria-label", label);
  parts.card.classList.toggle("small", group === "done");

  const open = expanded.has(job.id);
  parts.head.setAttribute("aria-expanded", String(open));
  parts.tracks.hidden = !open;
  if (open) {
    const rows = job.items.map((item) => {
      const row = el("div", "dl-trk");
      const status = el("span", `dl-trk-st ${item.status}${item.warning ? " warn" : ""}`,
        item.warning ? "Done, no identity" : STEP[item.status] || item.status);
      const name = el("span", "dl-trk-name", item.title);
      if (item.error || item.warning) name.title = item.error || item.warning;
      row.append(name, status);
      return row;
    });
    if (group === "attention" && job.items.some((i) => ["failed", "cancelled"].includes(i.status))) {
      const retry = el("button", "ghost dl-retry", "Retry failed");
      retry.type = "button";
      retry.addEventListener("click", () => retryJob(job.id, retry));
      rows.push(retry);
    }
    parts.tracks.replaceChildren(...rows);
  }
  return parts;
}

async function deleteJob(id) {
  try {
    const response = await fetch(`/api/jobs/${id}`, { method: "DELETE" });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      showError(body.detail || "Could not remove that download.");
    }
  } catch {
    showError("Could not reach the server.");
  }
}

async function retryJob(id, button) {
  button.disabled = true;
  try {
    const response = await fetch(`/api/jobs/${id}/retry`, { method: "POST" });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      showError(body.detail || "Could not retry that.");
      button.disabled = false;
    }
  } catch {
    showError("Could not reach the server.");
    button.disabled = false;
  }
}

/* --- the three shelves ------------------------------------------------------ */

let showFinished = false;
try { showFinished = localStorage.getItem("downloads.finished") === "open"; } catch { /* fine */ }

const activeBox = el("div", "dl-group");
const nothing = el("p", "dl-nothing",
  "Nothing downloading. Press the download button on anything you find.");
const attentionHead = el("h3", "dl-group-head", "Needs a look");
const attentionBox = el("div", "dl-group");
const doneHead = el("div", "dl-group-head");
const doneToggle = el("button", "dl-twist");
doneToggle.type = "button";
const doneClear = el("button", "ghost dl-clear", "Clear");
doneClear.type = "button";
doneClear.title = "Clear every finished download from this list. Nothing is deleted from the library.";
doneHead.append(doneToggle, doneClear);
const doneBox = el("div", "dl-group");
listEl.append(activeBox, nothing, attentionHead, attentionBox, doneHead, doneBox);

doneToggle.addEventListener("click", () => {
  showFinished = !showFinished;
  try { localStorage.setItem("downloads.finished", showFinished ? "open" : "closed"); } catch { /* fine */ }
  render();
});
doneClear.addEventListener("click", () => {
  [...jobs.values()].filter((job) => jobGroup(job) === "done").forEach((job) => deleteJob(job.id));
});

// Only touches the DOM where the order actually differs.
function place(box, cards) {
  cards.forEach((card, index) => {
    if (box.children[index] !== card) box.insertBefore(card, box.children[index] || null);
  });
  while (box.children.length > cards.length) box.lastChild.remove();
}

function render() {
  const ordered = [...jobs.values()].sort((a, b) => b.created_at.localeCompare(a.created_at));
  nodes.forEach((parts, id) => {
    if (!jobs.has(id)) {
      parts.card.remove();
      nodes.delete(id);
      expanded.delete(id);
    }
  });

  const shelves = { active: [], attention: [], done: [] };
  // Oldest first while moving, so the one being worked on sits at the top
  // and new arrivals queue up below it.
  [...ordered].reverse().filter((j) => jobGroup(j) === "active").forEach((j) => shelves.active.push(j));
  ordered.filter((j) => jobGroup(j) !== "active").forEach((j) => shelves[jobGroup(j)].push(j));

  place(activeBox, shelves.active.map((j) => updateJob(j).card));
  place(attentionBox, shelves.attention.map((j) => updateJob(j).card));
  // Finished cards are not even built while the shelf is folded.
  place(doneBox, showFinished ? shelves.done.map((j) => updateJob(j).card) : []);

  nothing.hidden = shelves.active.length > 0;
  attentionHead.hidden = attentionBox.hidden = !shelves.attention.length;
  doneHead.hidden = !shelves.done.length;
  doneBox.hidden = !showFinished;
  doneToggle.textContent = `Finished · ${shelves.done.length}`;
  doneToggle.setAttribute("aria-expanded", String(showFinished));

  const moving = shelves.active.length;
  summaryEl.textContent = moving ? `${moving} in progress` : "";
  renderPill(shelves);
}

/* --- the phone's pill and sheet --------------------------------------------- */

function renderPill(shelves) {
  const { active, attention, done } = shelves;
  const words = [];
  if (active.length) words.push(el("span", "dl-pill-pulse"), `${active.length} downloading`);
  if (attention.length) {
    if (words.length) words.push(" · ");
    words.push(el("span", "tone-bad", `${attention.length} need${attention.length === 1 ? "s" : ""} a look`));
  }
  if (!words.length) words.push(`✓ ${plural(done.length, "download")} finished`);

  const covers = el("span", "dl-pill-covers");
  (active.length ? active : attention.length ? attention : done).slice(0, 3)
    .forEach((job) => covers.append(remoteArt(coverOf(job))));
  pillEl.replaceChildren(covers, ...words);
  pillEl.hidden = !jobs.size;
}

function openSheet(open) {
  view.classList.toggle("downloads-open", open);
  scrimEl.hidden = !open;
  pillEl.setAttribute("aria-expanded", String(open));
  if (open) closeEl.focus({ preventScroll: true });
  else pillEl.focus({ preventScroll: true });
}

pillEl.addEventListener("click", () => openSheet(true));
closeEl.addEventListener("click", () => openSheet(false));
scrimEl.addEventListener("click", () => openSheet(false));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && view.classList.contains("downloads-open")) openSheet(false);
});

onJobs(render);
// "3 min ago" goes stale on its own.
setInterval(() => { if (!view.hidden && showFinished) render(); }, 60000);
render();
