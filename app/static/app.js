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

  socket.addEventListener("close", (event) => {
    connEl.textContent = "offline";
    connEl.className = "conn offline";
    // 4401 is this server saying the session has gone - a restart signs
    // everyone out. Reconnecting cannot fix that, and doing so forever
    // leaves the page looking merely offline when it needs a sign-in.
    if (event.code === 4401) {
      started = false;
      if (healthTimer) clearInterval(healthTimer);
      healthTimer = null;
      warnEl.hidden = true;
      showError("");
      showSignin(true);
      return;
    }
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
  // These belong to the installation, not to a person. Showing an editable
  // form to someone who will be refused on save is worse than not offering
  // it at all.
  const mayEdit = values.editable !== false;
  Array.from(settingsForm.elements).forEach((field) => {
    field.disabled = !mayEdit;
  });
  settingsNote.textContent = mayEdit
    ? "" : "Only a Navidrome administrator can change these.";
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

// Only once there is a session, and only on an answer we actually got. Run
// at load it fired while signed out, read `spotify_configured` off a 401
// body, found it undefined and announced that credentials were missing -
// which reads as the sign-in having failed rather than as a note about
// Spotify.
async function checkSpotify() {
  try {
    const response = await fetch("/api/status");
    if (!response.ok) return;
    const status = await response.json();
    warnEl.hidden = Boolean(status.spotify_configured);
    if (!status.spotify_configured) {
      warnEl.textContent =
        "Spotify credentials are not configured. Add them in Settings.";
    }
  } catch {
    /* the queue will report its own errors; a missing banner is not one */
  }
}


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
    // Driven off the tabs themselves rather than a hand-kept list: a view
    // removed from the markup used to leave a name here that resolved to
    // null, and the resulting throw hid every panel at once.
    document.querySelectorAll(".tab").forEach((other) => {
      const section = document.getElementById(`view-${other.dataset.view}`);
      if (section) section.hidden = other.dataset.view !== view;
    });
    // Focus the search box only where a keyboard is already there. On a
    // phone this summoned the on-screen one the instant the tab was
    // tapped, covering half the screen and scrolling the page out from
    // under the thumb that tapped it. Asked of the pointer rather than
    // the width: a tablet in a wide window is still a touch device, and
    // a laptop in a narrow one still has a real keyboard.
    if (view === "browse" && matchMedia("(hover: hover) and (pointer: fine)").matches) {
      queryInput.focus();
    }
    if (view === "staging") loadStaging();
    if (view === "health") loadHealth();
    if (view === "dupes") loadDupes();
    if (view === "playlists") loadPlaylists();
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

/* --- staging -------------------------------------------------------------
   What beets has not been able to file. There is no separate to-do list: the
   folder is the queue, so it cannot disagree with what is actually there. */

const stagingEl = document.getElementById("staging");
const stagingEmpty = document.getElementById("staging-empty");
const stagingBadge = document.getElementById("staging-badge");

function stagingRow(entry) {
  const row = el("div", `staging-item${entry.settled ? "" : " unsettled"}`);
  row.append(
    el("span", "staging-kind", entry.kind),
    el("span", "staging-name", entry.name),
    el("span", "staging-meta",
       `${entry.tracks} file${entry.tracks === 1 ? "" : "s"} · ` +
       `${(entry.bytes / 1e6).toFixed(0)} MB · ${entry.age_days}d`)
  );
  if (!entry.settled) {
    // Still being written to. Importing now would file a partial album.
    row.append(el("span", "staging-note", "still arriving"));
  }
  return row;
}

async function loadStaging() {
  try {
    const data = await fetch("/api/staging").then((r) => r.json());
    const entries = data.entries || [];
    stagingEl.replaceChildren(...entries.map(stagingRow));
    stagingEmpty.textContent = entries.length
      ? "" : `Nothing waiting in ${data.staging || "staging"}.`;
    stagingEmpty.hidden = entries.length > 0;
    stagingBadge.textContent = entries.length || "";
    stagingBadge.hidden = !entries.length;
  } catch (err) {
    stagingEmpty.textContent = `Could not read staging: ${err.message}`;
    stagingEmpty.hidden = false;
  }
}

document.getElementById("staging-import").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "Importing…";
  try {
    const result = await fetch("/api/staging/import", { method: "POST" })
      .then((r) => r.json());
    if (!result.ran) showError(`Nothing to import: ${result.reason}`);
    await loadStaging();
  } catch (err) {
    showError(`Import failed: ${err.message}`);
  } finally {
    button.disabled = false;
    button.textContent = "Try importing now";
  }
});

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
// having opened the panel. Started only once there is a session, so an
// unauthenticated page does not sit hammering endpoints that will refuse it.
let healthTimer = null;

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

// --- sign in --------------------------------------------------------------
// Navidrome owns the accounts, so this only forwards credentials to it and
// keeps the session cookie it hands back. Which library a download lands in,
// whose stars a duplicate carries and who owns a new playlist all follow from
// who signed in, rather than from configuration.

const signinEl = document.getElementById("signin");
const signinForm = document.getElementById("signin-form");
const signinError = document.getElementById("signin-error");
const whoamiEl = document.getElementById("whoami");

let session = null;

function showSignin(show) {
  signinEl.hidden = !show;
  document.querySelector("header").hidden = show;
  document.querySelector("main").hidden = show;
  if (show) signinForm.elements.username.focus();
}

function applySession(me) {
  session = me;
  const libraries = (me.libraries || []).map((l) => l.name).join(", ");
  whoamiEl.textContent = libraries
    ? `${me.username} · ${libraries}`
    : `${me.username} · no library assigned`;
  showSignin(false);
}

async function checkSession() {
  try {
    const me = await fetch("/api/auth/me").then((r) => r.json());
    if (me.signed_in) {
      applySession(me);
      return true;
    }
  } catch {
    /* server unreachable; the sign-in form is still the right thing to show */
  }
  showSignin(true);
  return false;
}

signinForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  signinError.hidden = true;
  const button = signinForm.querySelector("button");
  button.disabled = true;
  try {
    const response = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: signinForm.elements.username.value,
        password: signinForm.elements.password.value,
      }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      signinError.textContent = body.detail || `Sign in failed (${response.status})`;
      signinError.hidden = false;
      return;
    }
    signinForm.reset();
    applySession({ signed_in: true, ...body });
    start();
  } finally {
    button.disabled = false;
  }
});

document.getElementById("signout").addEventListener("click", async () => {
  await fetch("/api/auth/logout", { method: "POST" }).catch(() => {});
  if (socket) socket.close();
  location.reload();
});



/* --- smart playlists ------------------------------------------------------
   Rules, not a fixed list of tracks. The server hands over the vocabulary a
   rule can be built from along with the playlists themselves, so the form
   cannot offer a field the server would then refuse - add one in
   app/playlists.py and it appears here without touching this file. */

const playlistsEl = document.getElementById("playlists");
const playlistsEmpty = document.getElementById("playlists-empty");
const playlistError = document.getElementById("playlist-error");
const playlistEditor = document.getElementById("playlist-editor");
const conditionsEl = document.getElementById("pl-conditions");
const plNote = document.getElementById("pl-note");
const plDelete = document.getElementById("pl-delete");

let vocabulary = null;
// null while creating, a playlist id while editing an existing one.
let editingId = null;

function showPlaylistError(message) {
  playlistError.textContent = message;
  playlistError.hidden = !message;
}

function fieldSpec(name) {
  return vocabulary ? vocabulary.fields.find((f) => f.name === name) : null;
}

function conditionRow(condition) {
  const row = el("div", "pl-condition");

  const field = el("select", "pl-field");
  vocabulary.fields.forEach((spec) => {
    const option = el("option", "", spec.label);
    option.value = spec.name;
    field.append(option);
  });
  field.value = (condition && condition.field) || vocabulary.fields[0].name;

  const operator = el("select", "pl-operator");
  const value = el("span", "pl-value");

  const drop = el("button", "ghost remove-condition", "✕");
  drop.type = "button";
  drop.title = "Remove this condition";
  drop.addEventListener("click", () => {
    row.remove();
    // A rule with no conditions matches the whole library, so the server
    // refuses one. Better said here than after a round trip.
    if (!conditionsEl.children.length) conditionsEl.append(conditionRow(null));
  });

  function fillOperators(selected) {
    const spec = fieldSpec(field.value);
    operator.replaceChildren();
    spec.operators.forEach((op) => {
      const option = el("option", "", op.label);
      option.value = op.name;
      operator.append(option);
    });
    operator.value = spec.operators.some((o) => o.name === selected)
      ? selected : spec.operators[0].name;
  }

  function fillValue(current) {
    const spec = fieldSpec(field.value);
    let input;
    if (spec.kind === "boolean") {
      input = el("select", "pl-input");
      [["true", "yes"], ["false", "no"]].forEach((pair) => {
        const option = el("option", "", pair[1]);
        option.value = pair[0];
        input.append(option);
      });
      input.value = (current === false || current === "false") ? "false" : "true";
    } else if (spec.kind === "library") {
      input = el("select", "pl-input");
      (vocabulary.libraries || []).forEach((library) => {
        const option = el("option", "", library.name);
        option.value = String(library.id);
        input.append(option);
      });
      if (current !== undefined && current !== null) input.value = String(current);
    } else if (spec.kind === "date" && operator.value.indexOf("InTheLast") >= 0) {
      // A date asked "in the last" wants a number of days; asked "before" it
      // wants a date. Same field, different box.
      input = el("input", "pl-input");
      input.type = "number";
      input.min = "1";
      input.placeholder = "days";
      input.value = current === undefined || current === null ? "" : current;
    } else if (spec.kind === "date") {
      input = el("input", "pl-input");
      input.type = "date";
      input.value = current === undefined || current === null ? "" : current;
    } else if (spec.kind === "number") {
      input = el("input", "pl-input");
      input.type = "number";
      input.value = current === undefined || current === null ? "" : current;
    } else {
      input = el("input", "pl-input");
      input.type = "text";
      input.value = current === undefined || current === null ? "" : current;
    }
    if (spec.hint) input.title = spec.hint;
    value.replaceChildren(input);
  }

  field.addEventListener("change", () => {
    // Operators and the kind of value box both belong to the field, so
    // changing it rebuilds them rather than leaving "starts with" sitting
    // on a play count.
    fillOperators(null);
    fillValue(null);
  });
  operator.addEventListener("change", () => fillValue(null));

  fillOperators(condition && condition.operator);
  fillValue(condition ? condition.value : null);
  row.append(field, operator, value, drop);
  return row;
}

function readCondition(row) {
  const input = row.querySelector(".pl-input");
  return {
    field: row.querySelector(".pl-field").value,
    operator: row.querySelector(".pl-operator").value,
    value: input ? input.value : "",
  };
}

function openEditor(playlist) {
  editingId = (playlist && playlist.id) || null;
  showPlaylistError("");
  plNote.textContent = "";

  playlistEditor.elements.name.value = (playlist && playlist.name) || "";
  playlistEditor.elements.comment.value = (playlist && playlist.comment) || "";
  document.getElementById("pl-public").checked = !!(playlist && playlist.public);

  const shape = playlist ? playlist.form : null;
  document.getElementById("pl-match-mode").value = (shape && shape.match) || "all";

  const sortSelect = document.getElementById("pl-sort");
  sortSelect.replaceChildren();
  const none = el("option", "", "nothing in particular");
  none.value = "";
  sortSelect.append(none);
  vocabulary.sorts.forEach((sort) => {
    const option = el("option", "", sort.label);
    option.value = sort.name;
    sortSelect.append(option);
  });
  sortSelect.value = (shape && shape.sort) || "";
  document.getElementById("pl-direction").value = (shape && shape.direction) || "desc";
  document.getElementById("pl-limit").value = (shape && shape.limit) || "";

  const rows = ((shape && shape.conditions) || []).map((c) => conditionRow(c));
  conditionsEl.replaceChildren.apply(
    conditionsEl, rows.length ? rows : [conditionRow(null)]);

  plDelete.hidden = !editingId;
  playlistEditor.hidden = false;
  playlistEditor.scrollIntoView({ block: "nearest" });
}

function closeEditor() {
  playlistEditor.hidden = true;
  editingId = null;
}

function describeRule(shape) {
  const spoken = shape.conditions.map((condition) => {
    const spec = fieldSpec(condition.field);
    if (!spec) return condition.field;
    const op = spec.operators.find((o) => o.name === condition.operator);
    let value = condition.value;
    if (spec.kind === "boolean") value = value ? "yes" : "no";
    if (spec.kind === "library") {
      const library = (vocabulary.libraries || [])
        .find((l) => String(l.id) === String(value));
      if (library) value = library.name;
    }
    // "in the last 7" is a number of days, and the sentence has to say so
    // - the operator label cannot, because the value box beside it is
    // sometimes a date instead.
    if (spec.kind === "date" && condition.operator.indexOf("InTheLast") >= 0) {
      value = `${value} days`;
    }
    return `${spec.label} ${op ? op.label : condition.operator} ${value}`;
  });
  const joined = spoken.join(shape.match === "all" ? ", and " : ", or ");
  const sort = vocabulary.sorts.find((s) => s.name === shape.sort);
  const tail = [];
  if (sort) tail.push(`sorted by ${sort.label.toLowerCase()}`);
  if (shape.limit) tail.push(`first ${shape.limit}`);
  return tail.length ? `${joined} — ${tail.join(", ")}` : joined;
}

function playlistCard(playlist) {
  const card = el("div", "playlist");

  const head = el("div", "playlist-head");
  head.append(el("span", "playlist-name", playlist.name));
  head.append(el("span", "playlist-count",
    `${playlist.song_count} track${playlist.song_count === 1 ? "" : "s"}`));
  if (playlist.public) head.append(el("span", "badge", "shared"));
  card.append(head);

  if (playlist.comment) card.append(el("div", "playlist-comment", playlist.comment));

  if (playlist.form) {
    card.append(el("div", "playlist-rule", describeRule(playlist.form)));
  } else {
    // Rules this form cannot represent are shown and left alone. Opening one
    // would drop the part the form has no row for, and a smart playlist
    // quietly matching the wrong thing is worse than one we decline to edit.
    card.append(el("div", "playlist-rule dim",
      `Written by hand — ${playlist.unsupported}. Edit it where it was written.`));
  }

  const actions = el("div", "playlist-actions");
  if (playlist.form) {
    const edit = el("button", "ghost", "Edit");
    edit.type = "button";
    edit.addEventListener("click", () => openEditor(playlist));
    actions.append(edit);
  }
  card.append(actions);
  return card;
}

async function loadPlaylists() {
  try {
    const response = await fetch("/api/playlists");
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not load playlists.");
    vocabulary = data.vocabulary;
    playlistsEl.replaceChildren.apply(
      playlistsEl, data.playlists.map(playlistCard));
    playlistsEmpty.textContent = "No smart playlists yet.";
    playlistsEmpty.hidden = data.playlists.length > 0;
    showPlaylistError("");
  } catch (exc) {
    playlistsEl.replaceChildren();
    playlistsEmpty.hidden = true;
    showPlaylistError(String(exc.message || exc));
  }
}

document.getElementById("playlist-new").addEventListener("click", () => {
  if (!vocabulary) return;
  openEditor(null);
});

document.getElementById("pl-add-condition").addEventListener("click", () => {
  conditionsEl.append(conditionRow(null));
});

document.getElementById("pl-cancel").addEventListener("click", closeEditor);

playlistEditor.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = playlistEditor.querySelector("button[type=submit]");
  button.disabled = true;
  plNote.textContent = "";
  showPlaylistError("");

  const body = {
    name: playlistEditor.elements.name.value,
    comment: playlistEditor.elements.comment.value,
    public: document.getElementById("pl-public").checked,
    form: {
      match: document.getElementById("pl-match-mode").value,
      conditions: Array.from(conditionsEl.children).map(readCondition),
      sort: document.getElementById("pl-sort").value,
      direction: document.getElementById("pl-direction").value,
      limit: Number(document.getElementById("pl-limit").value) || 0,
    },
  };

  try {
    const response = await fetch(
      editingId ? `/api/playlists/${editingId}` : "/api/playlists",
      {
        method: editingId ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not save.");
    // The count comes back from Navidrome, which evaluated the rules on
    // save. It is the number the playlist will show there, because it is
    // the number that thing computed - not a second guess made here.
    plNote.textContent =
      `Saved — matches ${data.song_count} track${data.song_count === 1 ? "" : "s"}.`;
    await loadPlaylists();
  } catch (exc) {
    showPlaylistError(String(exc.message || exc));
  } finally {
    button.disabled = false;
  }
});

plDelete.addEventListener("click", async () => {
  if (!editingId) return;
  const name = playlistEditor.elements.name.value || "this playlist";
  if (!confirm(`Delete ${name}? No tracks are touched — only the rules.`)) return;
  try {
    const response = await fetch(`/api/playlists/${editingId}`, { method: "DELETE" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not delete.");
    closeEditor();
    await loadPlaylists();
  } catch (exc) {
    showPlaylistError(String(exc.message || exc));
  }
});

// Nothing runs until we know who is asking - the socket, the health poll and
// every panel are all views of somebody's library.
let started = false;

function start() {
  if (started) return;
  started = true;
  connect();
  loadHealth();
  loadStaging();
  checkSpotify();
  healthTimer = setInterval(loadHealth, 5 * 60 * 1000);
}

checkSession().then((signedIn) => {
  if (signedIn) start();
});
