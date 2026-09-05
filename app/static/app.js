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
  const secrets = ["spotify_client_secret", "navidrome_password"];
  Object.entries(values).forEach(([key, value]) => {
    const field = settingsForm.elements[key];
    if (field && !secrets.includes(key)) field.value = value;
  });
  // Secrets are never sent back, only whether one is set.
  secrets.forEach((key) => {
    const field = settingsForm.elements[key];
    if (field) field.placeholder = values[`${key}_set`] ? "unchanged" : "not set";
  });
});

settingsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = {};
  new FormData(settingsForm).forEach((value, key) => {
    if (value === "") return;
    payload[key] = ["concurrency", "max_attempts", "staging_sweep_minutes"].includes(key)
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


/* --- browse ------------------------------------------------------------- */

const resultsEl = document.getElementById("results");
const browseEmpty = document.getElementById("browse-empty");
const crumbEl = document.getElementById("crumb");
const queryInput = document.getElementById("query");
const searchForm = document.getElementById("search-form");

let kind = "album";
let searchToken = 0;

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    const view = tab.dataset.view;
    ["queue", "browse", "library", "health", "dupes"].forEach((name) => {
      document.getElementById(`view-${name}`).hidden = view !== name;
    });
    if (view === "browse") queryInput.focus();
    if (view === "library") openLibraryView();
    if (view === "health") loadHealth();
    if (view === "dupes") loadDupes();
  });
});

document.querySelectorAll(".kind").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".kind").forEach((k) => k.classList.toggle("active", k === button));
    kind = button.dataset.kind;
    if (queryInput.value.trim()) runSearch();
  });
});

function setBrowse(nodes, message) {
  resultsEl.replaceChildren(...nodes);
  browseEmpty.textContent = message || "";
  browseEmpty.hidden = nodes.length > 0 || !message;
}

function cover(url, className) {
  const box = el("div", `art ${className || ""}`);
  if (url) {
    const img = document.createElement("img");
    img.src = url;
    img.loading = "lazy";
    img.alt = "";
    box.append(img);
  }
  return box;
}

async function queue(url, button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Queued";
  const response = await fetch("/api/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    showError(body.detail || "Could not queue that.");
    button.disabled = false;
    button.textContent = original;
  }
}

function queueButton(url, label) {
  const button = el("button", "ghost queue", label || "Download");
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    queue(url, button);
  });
  return button;
}

function albumCard(card) {
  const node = el("div", "card");
  node.append(cover(card.cover));
  const body = el("div", "card-body");
  body.append(
    el("div", "card-title", card.name),
    el("div", "card-sub", `${card.artist}${card.year ? ` \u00b7 ${card.year}` : ""}`),
    el("div", "card-sub dim", `${card.total} track${card.total === 1 ? "" : "s"}`)
  );
  const actions = el("div", "card-actions");
  actions.append(queueButton(card.url, "Download"));
  const open = el("button", "ghost", "Tracks");
  open.addEventListener("click", (event) => {
    event.stopPropagation();
    openAlbum(card.id);
  });
  actions.append(open);
  body.append(actions);
  node.append(body);
  return node;
}

function trackCard(card) {
  const node = el("div", "card");
  node.append(cover(card.cover));
  const body = el("div", "card-body");
  body.append(
    el("div", "card-title", card.name),
    el("div", "card-sub", card.artist),
    el("div", "card-sub dim", `${card.album || ""}${card.year ? ` \u00b7 ${card.year}` : ""}`)
  );
  const actions = el("div", "card-actions");
  if (card.held) actions.append(el("span", "held", "already have"));
  actions.append(queueButton(card.url, "Download"));
  body.append(actions);
  node.append(body);
  return node;
}

function artistCard(card) {
  const node = el("div", "card");
  node.append(cover(card.cover, "round"));
  const body = el("div", "card-body");
  body.append(el("div", "card-title", card.name));
  if (card.followers != null) {
    body.append(el("div", "card-sub dim", `${card.followers.toLocaleString()} followers`));
  }
  const actions = el("div", "card-actions");
  const open = el("button", "ghost", "Albums");
  open.addEventListener("click", (event) => {
    event.stopPropagation();
    openArtist(card.id);
  });
  actions.append(open);
  body.append(actions);
  node.append(body);
  return node;
}

const RENDERERS = { album: albumCard, track: trackCard, artist: artistCard };

async function runSearch() {
  const q = queryInput.value.trim();
  if (!q) return;
  // Guards against a slow earlier request landing after a newer one.
  const token = ++searchToken;
  crumbEl.hidden = true;
  setBrowse([], "Searching\u2026");
  try {
    const data = await fetch(
      `/api/search?q=${encodeURIComponent(q)}&type=${kind}`
    ).then((r) => r.json());
    if (token !== searchToken) return;
    if (data.detail) return setBrowse([], data.detail);
    setBrowse(data.results.map(RENDERERS[data.type]), "Nothing found.");
  } catch {
    if (token === searchToken) setBrowse([], "Search failed.");
  }
}

function crumb(text, onBack) {
  crumbEl.replaceChildren();
  const back = el("button", "ghost", "\u2190 Back");
  back.addEventListener("click", onBack);
  crumbEl.append(back, el("span", "crumb-text", text));
  crumbEl.hidden = false;
}

async function openAlbum(id) {
  setBrowse([], "Loading\u2026");
  const data = await fetch(`/api/albums/${id}`).then((r) => r.json());
  if (data.detail) return setBrowse([], data.detail);

  crumb(`${data.artist} \u2014 ${data.name}`, runSearch);

  const header = el("div", "detail");
  header.append(cover(data.cover, "large"));
  const info = el("div", "detail-body");
  info.append(
    el("div", "detail-title", data.name),
    el("div", "card-sub", `${data.artist}${data.year ? ` \u00b7 ${data.year}` : ""}`),
    el("div", "card-sub dim",
      `${data.tracks.length} tracks${data.held_count ? ` \u00b7 ${data.held_count} already held` : ""}`)
  );
  info.append(queueButton(data.url, "Download album"));
  header.append(info);

  const list = el("div", "tracklist");
  data.tracks.forEach((track) => {
    const row = el("div", `track${track.held ? " skipped" : ""}`);
    row.append(
      el("span", "track-no", track.track_no ?? ""),
      el("span", "track-name", track.name),
      el("span", "track-dur", duration(track.duration_ms))
    );
    row.append(track.held
      ? el("span", "held", "have")
      : queueButton(track.url, "Get"));
    list.append(row);
  });

  resultsEl.classList.remove("grid");
  resultsEl.replaceChildren(header, list);
  browseEmpty.hidden = true;
}

async function openArtist(id) {
  setBrowse([], "Loading\u2026");
  const data = await fetch(`/api/artists/${id}/albums`).then((r) => r.json());
  if (data.detail) return setBrowse([], data.detail);
  crumb(data.artist.name, runSearch);
  resultsEl.classList.add("grid");
  setBrowse(data.albums.map(albumCard), "No albums found.");
}

searchForm.addEventListener("submit", (event) => {
  event.preventDefault();
  resultsEl.classList.add("grid");
  runSearch();
});

/* --- library and inbox --------------------------------------------------- */

const statsEl = document.getElementById("stats");
const inboxEl = document.getElementById("inbox");
const inboxSection = document.getElementById("inbox-section");
const libraryResults = document.getElementById("library-results");
const libraryEmpty = document.getElementById("library-empty");
const libraryForm = document.getElementById("library-form");
const libraryQuery = document.getElementById("library-query");

let libraryLoaded = false;

function hhmm(seconds) {
  if (!seconds) return "0h";
  const hours = Math.floor(seconds / 3600);
  return hours >= 1 ? `${hours}h` : `${Math.round(seconds / 60)}m`;
}

async function loadStats() {
  try {
    const s = await fetch("/api/library/stats").then((r) => r.json());
    statsEl.replaceChildren(
      stat(s.albums, "albums"),
      stat(s.tracks, "tracks"),
      stat(s.artists, "artists"),
      stat(hhmm(s.seconds), "runtime")
    );
  } catch {
    statsEl.replaceChildren();
  }
}

function stat(value, label) {
  const box = el("div", "stat");
  box.append(el("div", "stat-value", String(value)), el("div", "stat-label", label));
  return box;
}

/* --- inbox --------------------------------------------------------------- */

async function loadInbox() {
  let entries = [];
  try {
    entries = await fetch("/api/inbox").then((r) => r.json());
  } catch {
    entries = [];
  }
  inboxSection.hidden = entries.length === 0;
  inboxEl.replaceChildren(...entries.map(inboxRow));
}

function inboxRow(entry) {
  const row = el("div", "inbox-item");

  const head = el("div", "inbox-head");
  head.append(
    el("span", "inbox-name", entry.name),
    el("span", "badge", entry.kind),
    el("span", "card-sub dim", `${entry.tracks} track${entry.tracks === 1 ? "" : "s"}`)
  );

  const actions = el("div", "inbox-actions");
  const body = el("div", "inbox-body");
  body.hidden = true;

  const identify = el("button", "ghost", "Identify");
  identify.addEventListener("click", async () => {
    body.hidden = false;
    body.replaceChildren(el("p", "card-sub", "Asking MusicBrainz…"));
    identify.disabled = true;
    try {
      const data = await fetch(
        `/api/inbox/candidates?path=${encodeURIComponent(entry.path)}`
      ).then((r) => r.json());
      body.replaceChildren(candidateList(entry, data));
    } catch {
      body.replaceChildren(el("p", "card-sub", "Lookup failed."));
    }
    identify.disabled = false;
  });

  const asIs = el("button", "ghost", "Import as-is");
  asIs.title = "Keep the existing tags and file it without matching";
  asIs.addEventListener("click", () => applyChoice(entry, { as_is: true }, asIs));

  const drop = el("button", "ghost cancel", "Discard");
  drop.addEventListener("click", async () => {
    if (!confirm(`Delete "${entry.name}" without importing it?`)) return;
    await fetch("/api/inbox/discard", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: entry.path }),
    });
    loadInbox();
  });

  actions.append(identify, asIs, drop);
  head.append(actions);
  row.append(head, body);
  return row;
}

function candidateList(entry, data) {
  const wrap = el("div", "candidates");
  wrap.append(
    el("div", "card-sub dim",
      `tagged as "${data.current_artist || "?"} — ${data.current_album || "?"}", ` +
      `${data.file_count} files · beets says: ${data.recommendation}`)
  );

  if (!data.candidates || !data.candidates.length) {
    wrap.append(el("p", "card-sub", "No candidates found."));
    return wrap;
  }

  data.candidates.forEach((c) => {
    const row = el("div", "candidate");
    // beets' distance is a disagreement score; invert it for readability.
    const confidence = Math.round((1 - c.distance) * 100);
    const bits = [c.year, c.country, c.label, c.media].filter(Boolean).join(" · ");
    const gaps = [];
    if (c.missing) gaps.push(`${c.missing} missing`);
    if (c.unmatched) gaps.push(`${c.unmatched} unmatched`);

    const info = el("div", "candidate-info");
    info.append(
      el("div", "card-title", `${c.artist} — ${c.album}`),
      el("div", "card-sub dim", `${bits}${bits ? " · " : ""}${c.track_count} tracks` +
        (gaps.length ? ` · ${gaps.join(", ")}` : ""))
    );

    const score = el("span", `score ${confidence >= 90 ? "good" : confidence >= 70 ? "ok" : "poor"}`,
      `${confidence}%`);

    const pick = el("button", "ghost", "Use this");
    pick.addEventListener("click", () => applyChoice(entry, { album_id: c.album_id }, pick));

    row.append(score, info, pick);
    wrap.append(row);
  });
  return wrap;
}

async function applyChoice(entry, payload, button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Importing…";
  try {
    const result = await fetch("/api/inbox/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: entry.path, ...payload }),
    }).then((r) => r.json());

    if (result.ok) {
      await loadInbox();
      await loadStats();
      await loadLibrary(libraryQuery.value.trim());
    } else {
      showError(result.detail || result.output || "Import failed.");
      button.disabled = false;
      button.textContent = original;
    }
  } catch {
    showError("Could not reach the server.");
    button.disabled = false;
    button.textContent = original;
  }
}

/* --- library browsing ---------------------------------------------------- */

async function loadLibrary(query) {
  libraryEmpty.textContent = "Loading…";
  libraryEmpty.hidden = false;
  libraryResults.replaceChildren();
  try {
    const response = await fetch(
      `/api/library/albums?q=${encodeURIComponent(query || "")}`
    );
    const data = await response.json();
    if (!response.ok) {
      libraryEmpty.textContent = data.detail || "Query failed.";
      return;
    }
    libraryResults.replaceChildren(...data.map(libraryAlbum));
    libraryEmpty.textContent = data.length ? "" : "Nothing matches that query.";
    libraryEmpty.hidden = data.length > 0;
  } catch {
    libraryEmpty.textContent = "Could not read the library.";
  }
}

function libraryAlbum(album) {
  const row = el("div", "lib-album");
  const year = album.original_year || album.year;
  row.append(
    el("span", "lib-artist", album.albumartist || "Unknown"),
    el("span", "lib-title", album.album || "Unknown"),
    el("span", "lib-year", year ? String(year) : ""),
    el("span", "lib-count", `${album.tracks}`)
  );
  if (album.mb_albumid) {
    const tick = el("span", "lib-mb", "MB");
    tick.title = `MusicBrainz release ${album.mb_albumid}`;
    row.append(tick);
  } else {
    row.append(el("span", "lib-mb dim", "—"));
  }

  const tracks = el("div", "tracklist");
  tracks.hidden = true;
  let loaded = false;

  row.addEventListener("click", async () => {
    tracks.hidden = !tracks.hidden;
    if (loaded || tracks.hidden) return;
    loaded = true;
    const detail = await fetch(`/api/library/albums/${album.id}`).then((r) => r.json());
    tracks.replaceChildren(
      ...detail.tracks.map((t) => {
        const line = el("div", "track");
        line.append(
          el("span", "track-no", t.track ?? ""),
          el("span", "track-name", t.title),
          el("span", "track-artist", t.artist),
          el("span", "track-dur", duration((t.length || 0) * 1000)),
          el("span", "track-status", `${Math.round((t.bitrate || 0) / 1000)}k`)
        );
        return line;
      })
    );
  });

  const wrap = el("div", "lib-row");
  wrap.append(row, tracks);
  return wrap;
}

libraryForm.addEventListener("submit", (event) => {
  event.preventDefault();
  loadLibrary(libraryQuery.value.trim());
});

function openLibraryView() {
  loadStats();
  loadInbox();
  if (!libraryLoaded) {
    libraryLoaded = true;
    loadLibrary("");
  }
}

connect();

// --- health ---------------------------------------------------------------
// A list of numbers that should be zero. The badge on the tab is the whole
// point: problems should be visible without anyone going looking, because
// everything this catches is the kind of thing that stays quiet for months.

const healthEl = document.getElementById("health");
const healthEmpty = document.getElementById("health-empty");
const healthError = document.getElementById("health-error");
const healthBadge = document.getElementById("health-badge");

function renderCheck(check) {
  const row = el("div", `check ${check.status}`);
  const label = el("span", "check-label", check.label);
  if (check.hint) label.title = check.hint;

  row.append(
    label,
    el("span", "check-value", String(check.value)),
    el("span", "check-detail", check.detail || "")
  );
  return row;
}

function renderHealth(report) {
  healthError.textContent = report.navidrome_error
    ? `Navidrome database unreadable: ${report.navidrome_error}`
    : "";
  healthError.hidden = !report.navidrome_error;

  healthEl.replaceChildren(
    ...report.sections.map((section) => {
      const block = el("section", "check-group");
      block.append(el("h2", "section-head", section.title));
      block.append(...section.checks.map(renderCheck));
      return block;
    })
  );
  healthEmpty.hidden = report.sections.length > 0;

  healthBadge.textContent = report.problems || "";
  healthBadge.hidden = !report.problems;
}

async function loadHealth() {
  try {
    const response = await fetch("/api/health");
    if (!response.ok) throw new Error(await response.text());
    renderHealth(await response.json());
  } catch (err) {
    healthEmpty.textContent = `Could not run health checks: ${err.message}`;
    healthEmpty.hidden = false;
  }
}

// Poll quietly in the background so the tab badge is current without anyone
// having opened the panel.
loadHealth();
setInterval(loadHealth, 5 * 60 * 1000);

// Reading tags from every file takes long enough that it runs on a timer in
// the background; this is for when you have just fixed something and want the
// answer now rather than in six hours.
document.getElementById("health-audit").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "Reading…";
  try {
    await fetch("/api/health/audit", { method: "POST" });
    await loadHealth();
  } catch (err) {
    showError(`Audit failed: ${err.message}`);
  } finally {
    button.disabled = false;
    button.textContent = "Re-read files";
  }
});

// --- duplicates -----------------------------------------------------------
// Groups of files that look like the same recording. The keeper is chosen on
// quality alone, because stars are migrated onto it rather than protected in
// place - so the better file wins even when the worse one is the starred one.

const dupesEl = document.getElementById("dupes");
const dupesEmpty = document.getElementById("dupes-empty");
const dupeNote = document.getElementById("dupe-note");
const dupeBadge = document.getElementById("dupe-badge");

let dupeGroups = [];

function describeCopy(copy) {
  const bits = [copy.suffix];
  if (copy.bit_rate) bits.push(`${copy.bit_rate}k`);
  bits.push(`${copy.duration}s`);
  if (copy.size) bits.push(`${(copy.size / 1e6).toFixed(1)}MB`);
  return bits.join(" · ");
}

function renderGroup(group) {
  const card = el("div", `dupe${group.confident ? " confident" : ""}`);
  const first = group.copies[0];

  const head = el("div", "dupe-head");
  head.append(
    el("span", "dupe-title", `${first.artist} — ${first.title}`),
    el("span", "dupe-reason", group.reason === "musicbrainz"
      ? "same MusicBrainz recording" : "same title and length")
  );
  if (group.why) head.append(el("span", "dupe-why", `keep ${group.why}`));
  card.append(head);

  const name = `dupe-${group.key}`;
  group.copies.forEach((copy) => {
    const row = el("label", "dupe-copy");
    const radio = el("input");
    radio.type = "radio";
    radio.name = name;
    radio.value = copy.id;
    radio.checked = copy.id === group.keeper;

    row.append(
      radio,
      el("span", "dupe-spec", describeCopy(copy)),
      el("span", "dupe-album", copy.album || "—"),
      el("span", "dupe-path", copy.path)
    );
    if (copy.starred) row.append(el("span", "dupe-star", "★"));
    if (copy.rating) row.append(el("span", "dupe-star", "●".repeat(copy.rating)));
    card.append(row);
  });

  const actions = el("div", "dupe-actions");
  actions.append(
    action("Keep selected, remove the rest", "primary", async () => {
      const chosen = card.querySelector(`input[name="${CSS.escape(name)}"]:checked`);
      if (!chosen) return;
      await postDupe("/api/duplicates/resolve",
        { key: group.key, keeper: chosen.value });
    }),
    action("Keep both", "", async () => {
      await postDupe("/api/duplicates/dismiss", { key: group.key });
    })
  );
  card.append(actions);
  return card;
}

async function postDupe(path, body) {
  showError("");
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      showError(detail.detail || `Request failed (${response.status})`);
      return;
    }
    await loadDupes();
  } catch {
    showError("Could not reach the server.");
  }
}

function renderDupes(payload) {
  dupeGroups = payload.groups;
  dupesEl.replaceChildren(...dupeGroups.map(renderGroup));
  dupesEmpty.textContent = dupeGroups.length ? "" : "No duplicates found.";
  dupesEmpty.hidden = dupeGroups.length > 0;

  dupeBadge.textContent = dupeGroups.length || "";
  dupeBadge.hidden = !dupeGroups.length;

  dupeNote.textContent = payload.confident
    ? `${payload.confident} group(s) share a MusicBrainz recording id and can be resolved in one go.`
    : "";
  dupeNote.hidden = !payload.confident;
}

async function loadDupes() {
  try {
    const response = await fetch("/api/duplicates");
    if (!response.ok) throw new Error((await response.json()).detail || response.status);
    renderDupes(await response.json());
  } catch (err) {
    dupesEmpty.textContent = `Could not list duplicates: ${err.message}`;
    dupesEmpty.hidden = false;
  }
}

document.getElementById("dupe-auto").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const preview = await fetch("/api/duplicates/auto", { method: "POST" })
      .then((r) => r.json());
    if (!preview.eligible) {
      showError("Nothing is confident enough to resolve unattended.");
      return;
    }
    if (!confirm(`Resolve ${preview.eligible} group(s) that share a MusicBrainz recording id?\n\nThe lower-quality copy of each moves to duplicates-removed/.`)) return;
    await fetch("/api/duplicates/auto?apply=true", { method: "POST" });
    await loadDupes();
  } finally {
    button.disabled = false;
  }
});
