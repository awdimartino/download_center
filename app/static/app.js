"use strict";

const jobsEl = document.getElementById("jobs");
const emptyEl = document.getElementById("empty");
const errorEl = document.getElementById("error");
const warnEl = document.getElementById("warn");
const connEl = document.getElementById("conn");
const form = document.getElementById("add-form");
const urlInput = document.getElementById("url");

// Job id -> job. The server is authoritative; this is only a render cache.
const jobs = new Map();
// Jobs the user collapsed, so a re-render does not spring them back open.
const collapsed = new Set();

function duration(ms) {
  if (!ms) return "";
  const total = Math.round(ms / 1000);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

function showError(message) {
  errorEl.textContent = message;
  errorEl.hidden = !message;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

const ACTIVE = new Set(["matching", "downloading", "tagging", "retrying"]);

function renderTrack(item) {
  const row = el("div", `track ${item.status}`);

  const name = el("span", "track-name", item.title);
  if (item.error) name.title = item.error;

  const status = el("span", `track-status ${item.status}`, item.status);

  row.append(
    el("span", "track-no", item.track_no ?? ""),
    name,
    el("span", "track-artist", item.artist),
    el("span", "track-dur", duration(item.duration_ms)),
    status
  );

  // A progress bar only means something while bytes are moving.
  if (item.status === "downloading" && item.progress > 0) {
    const bar = el("div", "bar");
    const fill = el("div", "bar-fill");
    fill.style.width = `${Math.round(item.progress * 100)}%`;
    bar.append(fill);
    row.append(bar);
  }
  if (ACTIVE.has(item.status)) row.classList.add("active");
  return row;
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

async function call(path) {
  showError("");
  try {
    const response = await fetch(path, { method: "POST" });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      showError(body.detail || `Request failed (${response.status})`);
    }
  } catch {
    showError("Could not reach the server.");
  }
}

function action(label, className, handler) {
  const button = el("button", `ghost ${className}`, label);
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    handler();
  });
  return button;
}

function renderJob(job) {
  const card = el("div", "job");
  card.dataset.id = job.id;

  const head = el("div", "job-head");
  head.append(
    el("span", "job-title", job.title || job.source_url),
    el("span", "job-meta", job.items.length ? summarise(job.items) : ""),
    el("span", `badge ${job.status}`, job.status)
  );

  const remove = el("button", "remove", "\u00d7");
  remove.title = "Remove";
  remove.addEventListener("click", (event) => {
    event.stopPropagation();
    fetch(`/api/jobs/${job.id}`, { method: "DELETE" }).catch(() => {});
  });
  head.append(remove);

  const tracks = el("div", "tracks");
  tracks.hidden = collapsed.has(job.id);
  job.items.forEach((item) => tracks.append(renderTrack(item)));

  head.addEventListener("click", () => {
    tracks.hidden = !tracks.hidden;
    if (tracks.hidden) collapsed.add(job.id);
    else collapsed.delete(job.id);
  });

  card.append(head, tracks);
  if (job.error) card.append(el("div", "job-error", job.error));
  return card;
}

function render() {
  const ordered = [...jobs.values()].sort((a, b) =>
    b.created_at.localeCompare(a.created_at)
  );
  jobsEl.replaceChildren(...ordered.map(renderJob));
  emptyEl.hidden = ordered.length > 0;
}

function handleMessage(message) {
  if (message.type === "snapshot") {
    jobs.clear();
    message.jobs.forEach((job) => jobs.set(job.id, job));
  } else if (message.type === "job") {
    jobs.set(message.job.id, message.job);
  } else if (message.type === "job_deleted") {
    jobs.delete(message.id);
    collapsed.delete(message.id);
  } else {
    return;
  }
  render();
}

let socket = null;
let retryDelay = 1000;

function connect() {
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

  socket.addEventListener("close", () => {
    connEl.textContent = "offline";
    connEl.className = "conn offline";
    // Back off to at most 15s so a restarting server is picked up quickly
    // without hammering it while it is down.
    setTimeout(connect, retryDelay);
    retryDelay = Math.min(retryDelay * 2, 15000);
  });
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const url = urlInput.value.trim();
  if (!url) return;

  showError("");
  const button = form.querySelector("button");
  button.disabled = true;
  try {
    const response = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      showError(body.detail || `Request failed (${response.status})`);
    } else {
      urlInput.value = "";
    }
  } catch {
    showError("Could not reach the server.");
  } finally {
    button.disabled = false;
  }
});

const settingsForm = document.getElementById("settings");
const settingsNote = document.getElementById("settings-note");

document.getElementById("settings-toggle").addEventListener("click", async () => {
  settingsForm.hidden = !settingsForm.hidden;
  if (settingsForm.hidden) return;
  settingsNote.textContent = "";
  const values = await fetch("/api/settings").then((r) => r.json());
  Object.entries(values).forEach(([key, value]) => {
    const field = settingsForm.elements[key];
    if (field && key !== "spotify_client_secret") field.value = value;
  });
  settingsForm.elements.spotify_client_secret.placeholder =
    values.spotify_client_secret_set ? "unchanged" : "not set";
});

settingsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = {};
  new FormData(settingsForm).forEach((value, key) => {
    if (value === "") return;
    payload[key] = ["concurrency", "max_attempts"].includes(key)
      ? parseInt(value, 10)
      : key === "rate_limit_sleep"
      ? parseFloat(value)
      : value;
  });
  const response = await fetch("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (response.ok) {
    settingsNote.textContent = "Saved.";
    settingsForm.elements.spotify_client_secret.value = "";
    warnEl.hidden = true;
  } else {
    const body = await response.json().catch(() => ({}));
    settingsNote.textContent = body.detail || "Could not save.";
  }
});

fetch("/api/status")
  .then((response) => response.json())
  .then((status) => {
    if (!status.spotify_configured) {
      warnEl.textContent =
        "Spotify credentials are not configured. Add them to config/config.toml and restart.";
      warnEl.hidden = false;
    }
  })
  .catch(() => {});

connect();
