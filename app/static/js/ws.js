"use strict";

import { el, duration } from "./core.js";
import { showOperation } from "./operations.js";

const jobsEl = document.getElementById("jobs");
const emptyEl = document.getElementById("empty");
const connEl = document.getElementById("conn");

// Job id -> job. The server is authoritative; this is only a render cache.
const jobs = new Map();
// Jobs the user collapsed, so a re-render does not spring them back open.
const collapsed = new Set();

const ACTIVE = new Set(["matching", "downloading", "tagging", "retrying"]);

function renderTrack(item) {
  const row = el("div", "track");

  const name = el("span", "track-name", item.title);
  const status = el("span", "track-status", "");

  // The bar is built once and hidden, rather than added and removed. It only
  // means something while bytes are moving, but creating it per update is
  // what made a progress tick cost a subtree.
  const bar = el("div", "bar");
  const fill = el("div", "bar-fill");
  bar.append(fill);

  row.append(
    el("span", "track-no", item.track_no ?? ""),
    name,
    el("span", "track-artist", item.artist),
    el("span", "track-dur", duration(item.duration_ms)),
    status,
    bar
  );
  row._parts = { name, status, bar, fill };
  updateTrack(row, item);
  return row;
}

// Patches the row in place. Only the fields the server sends in a progress
// delta can change: status, progress and error.
function updateTrack(row, item) {
  const parts = row._parts;
  if (!parts) return;

  row.className = `track ${item.status}${ACTIVE.has(item.status) ? " active" : ""}`;
  parts.status.className = `track-status ${item.status}`;
  parts.status.textContent = item.status;
  if (item.error) parts.name.title = item.error;
  else parts.name.removeAttribute("title");

  const moving = item.status === "downloading" && item.progress > 0;
  parts.bar.hidden = !moving;
  if (moving) parts.fill.style.width = `${Math.round(item.progress * 100)}%`;
}

function summarise(items) {
  const counts = {};
  items.forEach((i) => (counts[i.status] = (counts[i.status] || 0) + 1));
  const parts = [];
  const done = (counts.complete || 0) + (counts.skipped || 0);
  if (done) parts.push(`${done}/${items.length} done`);
  if (counts.skipped) parts.push(`${counts.skipped} already held`);
  if (counts.failed) parts.push(`${counts.failed} failed`);
  if (!parts.length) parts.push(`${items.length} track${items.length === 1 ? "" : "s"}`);
  return parts.join(" · ");
}

// Job id -> the nodes making up that card, so a re-render patches what
// changed instead of rebuilding it. The whole list used to be recreated on
// every message - for a 200-track playlist that is 200 row subtrees thrown
// away and rebuilt twice a second, on a phone, which also lost any text
// selection and scroll position inside the card.
const jobNodes = new Map();

function buildJob(job) {
  const card = el("div", "job");
  card.dataset.id = job.id;

  const title = el("span", "job-title", "");
  const meta = el("span", "job-meta", "");
  const badge = el("span", "badge", "");
  const head = el("div", "job-head");
  head.append(title, meta, badge);

  const remove = el("button", "remove", "×");
  remove.title = "Remove";
  remove.addEventListener("click", (event) => {
    event.stopPropagation();
    fetch(`/api/jobs/${job.id}`, { method: "DELETE" }).catch(() => {});
  });
  head.append(remove);

  const tracks = el("div", "tracks");
  head.addEventListener("click", () => {
    tracks.hidden = !tracks.hidden;
    if (tracks.hidden) collapsed.add(job.id);
    else collapsed.delete(job.id);
  });

  const error = el("div", "job-error", "");
  error.hidden = true;
  card.append(head, tracks, error);

  const nodes = { card, title, meta, badge, tracks, error, rows: new Map() };
  jobNodes.set(job.id, nodes);
  return nodes;
}

function updateJob(job) {
  const nodes = jobNodes.get(job.id) || buildJob(job);

  nodes.title.textContent = job.title || job.source_url;
  nodes.meta.textContent = job.items.length ? summarise(job.items) : "";
  nodes.badge.textContent = job.status;
  nodes.badge.className = `badge ${job.status}`;
  nodes.tracks.hidden = collapsed.has(job.id);
  nodes.error.textContent = job.error || "";
  nodes.error.hidden = !job.error;

  // Rows are keyed by item id, so an item list that only changed state
  // reuses every row it already had.
  const wanted = new Set();
  job.items.forEach((item) => {
    wanted.add(item.id);
    let row = nodes.rows.get(item.id);
    if (!row) {
      row = renderTrack(item);
      nodes.rows.set(item.id, row);
      nodes.tracks.append(row);
    } else {
      updateTrack(row, item);
    }
  });
  nodes.rows.forEach((row, id) => {
    if (!wanted.has(id)) {
      row.remove();
      nodes.rows.delete(id);
    }
  });
  return nodes;
}

function render() {
  const ordered = [...jobs.values()].sort((a, b) =>
    b.created_at.localeCompare(a.created_at)
  );

  // Drop cards for jobs that have gone.
  jobNodes.forEach((nodes, id) => {
    if (!jobs.has(id)) {
      nodes.card.remove();
      jobNodes.delete(id);
    }
  });

  ordered.forEach((job, index) => {
    const nodes = updateJob(job);
    // Only touch the DOM when the order actually differs.
    if (jobsEl.children[index] !== nodes.card) {
      jobsEl.insertBefore(nodes.card, jobsEl.children[index] || null);
    }
  });
  emptyEl.hidden = ordered.length > 0;
}

// A delta from the server: the job's status plus only the items that moved.
function applyProgress(message) {
  const job = jobs.get(message.id);
  if (!job) return;
  job.status = message.status;
  job.error = message.error;
  const byId = new Map(job.items.map((i) => [i.id, i]));
  message.items.forEach((patch) => {
    const item = byId.get(patch.id);
    if (item) Object.assign(item, patch);
  });
  render();
}

function handleMessage(message) {
  if (message.type === "snapshot") {
    jobs.clear();
    message.jobs.forEach((job) => jobs.set(job.id, job));
  } else if (message.type === "job") {
    jobs.set(message.job.id, message.job);
  } else if (message.type === "job_progress") {
    applyProgress(message);
    return;
  } else if (message.type === "job_deleted") {
    jobs.delete(message.id);
    collapsed.delete(message.id);
  } else if (message.type === "operation") {
    // Import and audit finish long after their request returned.
    showOperation(message.operation);
    return;
  } else {
    return;
  }
  render();
}

let socket = null;
let retryDelay = 1000;

// `onSessionExpired` is called instead of this module reaching into
// sign-in/bootstrap state directly - main.js owns started/healthTimer/the
// sign-in form, this module only knows "the server says the session is
// gone".
export function connect(onSessionExpired) {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${scheme}://${location.host}/ws`);

  socket.addEventListener("open", () => {
    retryDelay = 1000;
    connEl.textContent = "live";
    connEl.className = "conn online";
  });

  socket.addEventListener("message", (event) => {
    handleMessage(JSON.parse(event.data));
  });

  socket.addEventListener("close", (event) => {
    connEl.textContent = "offline";
    connEl.className = "conn offline";
    // 4401 is this server saying the session has gone - a restart signs
    // everyone out. Reconnecting cannot fix that, and doing so forever
    // leaves the page looking merely offline when it needs a sign-in.
    if (event.code === 4401) {
      onSessionExpired();
      return;
    }
    // Back off to at most 15s so a restarting server is picked up quickly
    // without hammering it while it is down.
    setTimeout(() => connect(onSessionExpired), retryDelay);
    retryDelay = Math.min(retryDelay * 2, 15000);
  });
}
