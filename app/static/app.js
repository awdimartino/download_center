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

  const remove = el("button", "remove", "\u00d7");
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

// A view like any other, loaded when it is shown. It used to be a form that
// toggled on top of whichever panel you were looking at, which meant Settings
// appeared above a list of duplicates and left you with no clear way back.
async function loadSettings() {
  settingsNote.textContent = "";
  const values = await fetch("/api/settings").then((r) => r.json());
  const secrets = ["spotify_client_secret", "navidrome_password"];
  // A non-admin is sent nothing but `editable: false` - these settings hold
  // the service credentials and decide where every library lives, so there
  // is nothing here for them to see and something to leak.
  Object.entries(values).forEach(([key, value]) => {
    const field = settingsForm.elements[key];
    if (field && !secrets.includes(key)) field.value = value ?? "";
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
}

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

/* --- navigation ----------------------------------------------------------
   One menu at every width, replacing the desktop tabs and the fixed bottom
   bar. The bar needed a 6rem overhang to cover the iOS home indicator, short
   labels behind a font-size:0 trick and badge positioning of its own; none
   of that survives, and there is one layout to keep working instead of two. */

const menu = document.getElementById("menu");
const menuToggle = document.getElementById("menu-toggle");
const menuBackdrop = document.getElementById("menu-backdrop");
const menuDot = document.getElementById("menu-dot");
const viewTitle = document.getElementById("current-view");
const navItems = () => document.querySelectorAll(".nav-item[data-view]");

// The panel hangs below the header rather than covering it, so it needs to
// know where the header ends. Measured rather than assumed: the height moves
// with the safe-area inset, the font size and the 720px breakpoint.
function syncHeaderHeight() {
  const header = document.querySelector("header");
  if (!header) return;
  document.documentElement.style.setProperty(
    "--header-h", `${Math.round(header.getBoundingClientRect().height)}px`);
}

syncHeaderHeight();
addEventListener("resize", syncHeaderHeight);
addEventListener("orientationchange", syncHeaderHeight);

function openMenu() {
  syncHeaderHeight();
  menu.hidden = false;
  menuToggle.setAttribute("aria-expanded", "true");
  menuToggle.setAttribute("aria-label", "Close menu");
  // The active section, so the menu opens on where you already are rather
  // than at the top of the list.
  const current = document.querySelector(".nav-item.active");
  if (current) current.focus();
}

function closeMenu() {
  if (menu.hidden) return;
  menu.hidden = true;
  menuToggle.setAttribute("aria-expanded", "false");
  menuToggle.setAttribute("aria-label", "Open menu");
  // Back to the control that opened it, or the focus ring is left on an
  // element that is now display:none and the next Tab starts from the top.
  menuToggle.focus();
}

menuToggle.addEventListener("click", () => {
  if (menu.hidden) openMenu();
  else closeMenu();
});
menuBackdrop.addEventListener("click", closeMenu);

document.addEventListener("keydown", (event) => {
  if (menu.hidden) return;
  if (event.key === "Escape") {
    closeMenu();
    return;
  }
  if (event.key !== "Tab") return;
  // Keep Tab inside the panel while it is open. Without this the focus ring
  // walks off into the page behind an opaque overlay, where it cannot be
  // seen and Enter presses something invisible.
  const focusable = [menuToggle, ...menu.querySelectorAll("button:not([disabled])")];
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
});

function showView(view) {
  // Driven off the buttons themselves rather than a hand-kept list: a view
  // removed from the markup used to leave a name here that resolved to
  // null, and the resulting throw hid every panel at once.
  navItems().forEach((item) => {
    const section = document.getElementById(`view-${item.dataset.view}`);
    if (section) section.hidden = item.dataset.view !== view;
    item.classList.toggle("active", item.dataset.view === view);
  });

  const active = document.querySelector(`.nav-item[data-view="${view}"]`);
  const label = active && active.querySelector(".nav-label");
  // With the tabs gone this is the only thing saying where you are.
  if (label) viewTitle.textContent = label.textContent;

  // Focus the search box only where a keyboard is already there. On a phone
  // this summoned the on-screen one the instant the tab was tapped, covering
  // half the screen and scrolling the page out from under the thumb that
  // tapped it. Asked of the pointer rather than the width: a tablet in a
  // wide window is still a touch device, and a laptop in a narrow one still
  // has a real keyboard.
  if (view === "browse" && matchMedia("(hover: hover) and (pointer: fine)").matches) {
    queryInput.focus();
  }
  if (view === "staging") loadStaging();
  if (view === "health") loadHealth();
  if (view === "dupes") loadDupes();
  if (view === "playlists") loadPlaylists();
  if (view === "settings") loadSettings();
}

navItems().forEach((item) => {
  item.addEventListener("click", () => {
    showView(item.dataset.view);
    closeMenu();
  });
});

// One place that sets a nav count, so the attention dot cannot fall out of
// step with the badges it summarises.
function setBadge(element, count) {
  element.textContent = count || "";
  element.hidden = !count;
  updateAttentionDot();
}

function updateAttentionDot() {
  const anything = [...document.querySelectorAll(".nav-item .badge")]
    .some((badge) => !badge.hidden);
  menuDot.hidden = !anything;
}

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

// "Already have" is the ledger's word, not the filesystem's - beets moved
// the file out of staging, so that table is the only thing that knows. When
// the file later leaves the library nothing notices, and the track becomes
// permanently unfetchable with no way to say otherwise. Clicking the tag is
// the way back.
//
// The tag replaces itself with a download button rather than re-running the
// view, so the answer is immediate and this does not need to know whether it
// is inside a search grid or an album listing.
function heldTag(item, queueLabel) {
  const tag = el("button", "held", queueLabel === "Get" ? "have" : "already have");
  tag.type = "button";
  tag.title = "Already downloaded into your library. Click to forget it, so "
            + "it can be downloaded again. No file is touched.";
  tag.addEventListener("click", async (event) => {
    event.stopPropagation();
    if (!confirm(
      `Forget "${item.name}"?\n\n`
      + "It stops counting as already downloaded, so it can be fetched "
      + "again.\nNothing on disk is touched.")) return;
    showError("");
    try {
      const response = await fetch("/api/ledger/forget", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_id: item.id }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        showError(body.detail || `Request failed (${response.status})`);
        return;
      }
      tag.replaceWith(queueButton(item.url, queueLabel));
    } catch {
      showError("Could not reach the server.");
    }
  });
  return tag;
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
  if (card.held) actions.append(heldTag(card, "Download"));
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
      ? heldTag(track, "Get")
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
    setBadge(stagingBadge, entries.length);
  } catch (err) {
    stagingEmpty.textContent = `Could not read staging: ${err.message}`;
    stagingEmpty.hidden = false;
  }
}

// Import and audit are started, not awaited - beets gets 900s per path and
// an audit reads every file in the library, both far longer than a browser
// will hold a request open. The server pushes the outcome over the socket.
const OPERATION_LABELS = {
  import: { button: "staging-import", idle: "Try importing now",
            busy: "Importing…" },
  audit: { button: "health-audit", idle: "Re-read files", busy: "Reading…" },
};

function showOperation(operation) {
  const spec = OPERATION_LABELS[operation.name];
  if (!spec) return;
  const button = document.getElementById(spec.button);
  if (button) {
    button.disabled = operation.status === "running";
    button.textContent = operation.status === "running" ? spec.busy : spec.idle;
  }
  if (operation.status === "running") return;

  if (operation.status === "failed") {
    showError(`${operation.name} failed: ${operation.error}`);
    return;
  }
  const result = operation.result || {};
  if (operation.name === "import") {
    if (result.busy) {
      showError("An import is already running; this one was not started.");
    } else if (!result.ran) {
      showError(`Nothing to import: ${result.reason}`);
    } else {
      const failed = result.failed || [];
      showError(failed.length ? `Import problems: ${failed.join("; ")}` : "");
    }
    loadStaging();
  } else {
    loadHealth();
  }
}

async function startOperation(name, path) {
  showError("");
  try {
    const payload = await fetch(path, { method: "POST" })
      .then((r) => r.json());
    if (payload.detail) {
      showError(payload.detail);
      return;
    }
    if (!payload.started) {
      showError(`That is already running; watching the one in flight.`);
    }
    showOperation(payload.operation);
  } catch (err) {
    showError(`Could not start: ${err.message}`);
  }
}

document.getElementById("staging-import").addEventListener("click", () => {
  startOperation("import", "/api/staging/import");
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

  setBadge(healthBadge, report.problems);
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
document.getElementById("health-audit").addEventListener("click", () => {
  startOperation("audit", "/api/health/audit");
});

// --- duplicates -----------------------------------------------------------
// Groups of files that look like the same recording. The keeper is chosen on
// quality alone, because stars are migrated onto it rather than protected in
// place - so the better file wins even when the worse one is the starred one.

const dupesEl = document.getElementById("dupes");
const dupesEmpty = document.getElementById("dupes-empty");
const dupeNote = document.getElementById("dupe-note");
const dupeBadge = document.getElementById("dupe-badge");
const dupeResult = document.getElementById("dupe-result");

let dupeGroups = [];

function describeCopy(copy) {
  const bits = [copy.suffix];
  if (copy.bit_rate) bits.push(`${copy.bit_rate}k`);
  bits.push(`${copy.duration}s`);
  if (copy.size) bits.push(`${(copy.size / 1e6).toFixed(1)}MB`);
  return bits.join(" · ");
}

// Titles differ within a group more often than the grouping admits: matching
// normalises away "feat." clauses, so two different collaborations of one
// song land together. Showing only the group's first title hid exactly the
// difference you need to see before removing one of them.
function titlesDiffer(copies) {
  return new Set(copies.map((c) => `${c.artist} — ${c.title}`)).size > 1;
}

function renderGroup(group) {
  const card = el("div", `dupe${group.confident ? " confident" : ""}`);
  const first = group.copies[0];
  const mixed = titlesDiffer(group.copies);

  const head = el("div", "dupe-head");
  head.append(
    el("span", "dupe-title", `${first.artist} — ${first.title}`),
    el("span", "dupe-reason", group.reason === "musicbrainz"
      ? "same MusicBrainz recording" : "same title and length")
  );
  if (group.why) head.append(el("span", "dupe-why", `keep ${group.why}`));
  card.append(head);
  if (mixed) {
    card.append(el("div", "dupe-warn",
      "These are not titled the same. Check they are the same recording " +
      "before removing either."));
  }

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
      // The per-copy title, always. It is the field the decision turns on.
      el("span", `dupe-copy-title${mixed ? " differs" : ""}`,
         `${copy.artist} — ${copy.title}`),
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
      const keeper = group.copies.find((c) => c.id === chosen.value);
      const losers = group.copies.filter((c) => c.id !== chosen.value);
      // Asked, because this moves audio files and there is no undo button.
      // Everything else that touches a file confirms; this was the one that
      // did not, and it is the one you press two hundred times.
      const lines = [
        `Remove ${losers.length} cop${losers.length === 1 ? "y" : "ies"}, keeping:`,
        `    ${keeper.artist} — ${keeper.title}`,
        `    ${describeCopy(keeper)}`,
        `    ${keeper.path}`,
        "",
        "Moving to duplicates-removed/ inside the library:",
        ...losers.map((c) => `    ${c.artist} — ${c.title}  (${describeCopy(c)})\n    ${c.path}`),
      ];
      if (mixed) lines.push("", "These copies are NOT titled the same.");
      if (!confirm(lines.join("\n"))) return;
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

// The server reports what it actually managed to do: which annotation it
// moved, which file it could not. Discarding that and simply reloading meant
// a failed move looked exactly like a successful one - the group disappeared
// from the list either way, whether or not anything had happened.
function reportResolution(payload) {
  if (!payload || payload.quarantined === undefined) {
    dupeResult.hidden = true;
    return;
  }
  const moved = payload.quarantined || [];
  const failed = payload.failed || [];
  const migrated = payload.migrated || [];
  const parts = [];
  if (moved.length) {
    parts.push(`Set aside ${moved.length} file${moved.length === 1 ? "" : "s"} ` +
               `to duplicates-removed/.`);
  }
  if (migrated.length) {
    parts.push(`Moved ${migrated.join(" and ")} onto the copy you kept.`);
  }
  if (failed.length) parts.push(`Could not move: ${failed.join("; ")}`);
  if (!moved.length && !failed.length) parts.push("Nothing was moved.");
  dupeResult.textContent = parts.join(" ");
  dupeResult.className = failed.length ? "warn" : "notice";
  dupeResult.hidden = false;
}

async function postDupe(path, body) {
  showError("");
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      showError(payload.detail || `Request failed (${response.status})`);
      return;
    }
    reportResolution(payload);
    await loadDupes();
    // Keep the set-aside list honest if it is open, since a resolve is
    // exactly the thing that adds to it.
    if (quarantineEl.open) await loadQuarantine();
  } catch {
    showError("Could not reach the server.");
  }
}

function renderDupes(payload) {
  dupeGroups = payload.groups;
  dupesEl.replaceChildren(...dupeGroups.map(renderGroup));
  dupesEmpty.textContent = dupeGroups.length ? "" : "No duplicates found.";
  dupesEmpty.hidden = dupeGroups.length > 0;

  setBadge(dupeBadge, dupeGroups.length);

  dupeNote.textContent = payload.confident
    ? `${payload.confident} group(s) share a MusicBrainz recording id and can be resolved in one go.`
    : "";
  dupeNote.hidden = !payload.confident;
}

// --- what has already been set aside -------------------------------------
// Read-only. It exists because "nothing is deleted" is a claim you should be
// able to check, and until now the only way to check it was to ssh in.

const quarantineEl = document.getElementById("quarantine");
const quarantineList = document.getElementById("quarantine-list");
const quarantineEmpty = document.getElementById("quarantine-empty");
const quarantineCount = document.getElementById("quarantine-count");

function bytes(n) {
  if (!n) return "";
  return n > 1e9 ? `${(n / 1e9).toFixed(1)} GB` : `${(n / 1e6).toFixed(0)} MB`;
}

function quarantineRow(entry) {
  const row = el("div", `quarantined${entry.present ? "" : " gone"}`);
  const named = entry.artist || entry.title;

  row.append(
    el("span", "quarantined-name",
       named ? `${entry.artist} — ${entry.title}` : entry.name),
    el("span", "quarantined-album", entry.album || "—"),
    el("span", "quarantined-path", entry.was || entry.path),
    el("span", "quarantined-size", bytes(entry.size))
  );

  const notes = [];
  if (!entry.present) notes.push("file no longer there");
  // A file with no ledger row was set aside before the record was kept, or
  // moved here by hand. Worth saying so rather than showing a blank line.
  if (!entry.recorded) notes.push("no record of who removed it");
  if (entry.decided_by) notes.push(`removed by ${entry.decided_by}`);
  if (notes.length) row.append(el("span", "quarantined-note", notes.join(" · ")));
  if (entry.kept) row.title = `Kept instead: ${entry.kept}`;
  return row;
}

async function loadQuarantine() {
  try {
    const data = await fetch("/api/duplicates/quarantined").then((r) => r.json());
    const entries = data.entries || [];
    quarantineList.replaceChildren(...entries.map(quarantineRow));

    quarantineCount.textContent = entries.length
      ? `${data.total}${data.truncated ? "+" : ""}${data.bytes ? ` · ${bytes(data.bytes)}` : ""}`
      : "none";
    quarantineEmpty.hidden = entries.length > 0;

    const caveats = [];
    if (data.missing) caveats.push(`${data.missing} recorded file(s) are no longer there`);
    if (data.unrecorded) caveats.push(`${data.unrecorded} predate the removal log`);
    quarantineEmpty.textContent = entries.length
      ? "" : "Nothing has been set aside.";
    if (caveats.length) {
      quarantineList.append(el("p", "panel-sub", caveats.join(" · ") + "."));
    }
  } catch (err) {
    quarantineEmpty.textContent = `Could not read the quarantine: ${err.message}`;
    quarantineEmpty.hidden = false;
  }
}

// Only when it is opened. It walks a directory, and most visits to this tab
// are about the list above it.
quarantineEl.addEventListener("toggle", () => {
  if (quarantineEl.open) loadQuarantine();
});

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
    if (!confirm(`Resolve ${preview.eligible} group(s) that share a MusicBrainz recording id?\n\nThe lower-quality copy of each moves to duplicates-removed/ inside its own library. This cannot be undone from here.`)) return;
    const result = await fetch("/api/duplicates/auto?apply=true", { method: "POST" })
      .then((r) => r.json());
    // Reported rather than discarded: a run that resolved nothing and a run
    // that resolved everything used to look identical from here.
    const failed = result.failed || [];
    dupeResult.textContent =
      `Resolved ${result.resolved || 0} group(s).` +
      (failed.length ? ` ${failed.length} problem(s): ${failed.slice(0, 3).join("; ")}` : "");
    dupeResult.className = failed.length ? "warn" : "notice";
    dupeResult.hidden = false;
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
  // A restart signs everyone out. Leaving the menu up over the sign-in form
  // would hide the only thing there is to do.
  if (show) closeMenu();
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
    // The editor is now editing the thing it just made. Without this it
    // still believed it was creating, so a second Save made a second
    // playlist, and a third made a third.
    if (!editingId && data.id) {
      editingId = data.id;
      plDelete.hidden = false;
    }
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
