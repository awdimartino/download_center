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

// Every banner in the application, raised the same way and closable. They
// used to be plain text set on a paragraph, which meant one could only be
// got rid of by doing something else that happened to overwrite it - so a
// message about a thing you had already dealt with sat there indefinitely.
//
// `tone` is the class: "error", "warn" or "notice". Passing an empty message
// clears the banner, which is how most callers reset one.
function setBanner(node, message, tone) {
  if (!node) return;
  if (!message) {
    node.replaceChildren();
    node.hidden = true;
    return;
  }
  node.className = tone || node.dataset.tone || "warn";
  const close = el("button", "banner-close", "×");
  close.type = "button";
  close.title = "Dismiss";
  close.setAttribute("aria-label", "Dismiss");
  close.addEventListener("click", () => setBanner(node, ""));
  node.replaceChildren(el("span", "banner-text", message), close);
  node.hidden = false;
}

function showError(message) {
  setBanner(errorEl, message, "error");
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
      setBanner(warnEl, "");
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
  const secrets = ["spotify_client_secret", "navidrome_password", "acoustid_key"];
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
    // Every secret field, not just the Spotify one. A value left sitting in
    // the form is submitted again on the next save, and a field the server
    // deliberately never sends back should not keep holding one either.
    ["spotify_client_secret", "navidrome_password", "acoustid_key"].forEach((key) => {
      const field = settingsForm.elements[key];
      if (field) field.value = "";
    });
    loadSettings();
    setBanner(warnEl, "");
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
    setBanner(
      warnEl,
      status.spotify_configured
        ? ""
        : "Spotify credentials are not configured. Add them in Settings.",
      "warn");
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

  // The banner belongs to whatever you were just doing. It lives outside
  // every section, so leaving it up carried one panel's problem onto all the
  // others, where there was nothing to act on and nothing to clear it.
  showError("");

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
  if (view === "home") loadHome();
  if (view === "library") loadLibrary();
  if (view === "health") loadHealth();
  if (view === "dupes") loadDupes();
  if (view === "playlists") loadPlaylists();
  if (view === "listening") loadListening();
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

// "In library" is read from Navidrome, so it means what it says: this
// recording is there now. It used to come from the download ledger, which
// recorded that a track had been *fetched* - and stayed true after the file
// was deleted, replaced or moved, leaving the track permanently unfetchable
// with nothing to say why. That is why there was a Forget button beside it,
// and why there no longer needs to be one: nothing is refused on the strength
// of this, so there is nothing to undo.
//
// Pinned left so it cannot shove the Download button out of line wherever it
// appears.
function heldMarker() {
  const marker = el("span", "held", "in library");
  marker.title = "Your library already has this. Downloading anyway is "
               + "allowed - it will arrive as a second copy.";
  return marker;
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
  actions.append(queueButton(card.url, "Download"));
  if (card.held) actions.prepend(heldMarker());
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
  // Results are always a card grid, so this belongs here rather than on the
  // submit handler. openAlbum takes the class off for its detail view, and
  // Back comes through runSearch without passing the form: every card then
  // laid out at its natural width, which is a cover image the size of the
  // screen. It looked like a rendering bug and was a missing class.
  resultsEl.classList.add("grid");
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
    // Same shape as a card: the Get button always sits in the last column,
    // and being held adds a marker and a Forget beside it rather than
    // standing in for it.
    const actions = el("div", "track-actions");
    actions.append(queueButton(track.url, "Get"));
    if (track.held) actions.prepend(heldMarker());
    row.append(actions);
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
  runSearch();
});

/* --- library --------------------------------------------------------------
   Every album you own, one row per folder, newest first. The filter narrows
   to albums MusicBrainz has never confirmed - which is derived from the
   files every time, never stored, so nothing here can fall out of step with
   what is true.

   It is a filter and not the list itself, because hiding matched albums is
   what made one unreachable: applying the wrong release gave it MusicBrainz
   ids, dropped it off the only list carrying the button, and left no way to
   correct it. */

const libraryEl = document.getElementById("library");
const libraryEmpty = document.getElementById("library-empty");
const libraryBadge = document.getElementById("library-badge");
const libraryCount = document.getElementById("library-count");
const libraryMore = document.getElementById("library-more");
const librarySearch = document.getElementById("library-search");
const libraryUnmatched = document.getElementById("library-unmatched");

// How much of the list is on screen.
//
// Paged by offset rather than by asking for an ever-larger limit: the server
// clamps a limit at MAX_PAGE, so growing it stopped having any effect past
// 200 albums and "Show more" re-fetched the same rows for ever. Each page is
// appended, so the ones already read stay put.
const LIBRARY_PAGE = 50;
let libraryShownCount = 0;

// Which album rows are open, by "library_id/folder", so re-rendering a page
// does not close what somebody was reading.
const libraryOpen = new Set();

function albumKey(album) {
  return `${album.library_id}/${album.folder}`;
}

// Cover art, proxied from Navidrome at the size asked for - it resizes, and
// the embedded images behind these are often a megabyte each.
//
// Lazy, so scrolling past two thousand albums does not fetch two thousand
// covers, and it removes itself if there is none rather than leaving a
// broken-image glyph in the row.
function cover(trackId, size) {
  const box = el("div", "art");
  if (!trackId) return box;
  const img = el("img");
  img.loading = "lazy";
  img.decoding = "async";
  img.alt = "";
  img.width = size;
  img.height = size;
  img.src = `/api/library/art?id=${encodeURIComponent(trackId)}&size=${size}`;
  img.addEventListener("error", () => img.remove());
  box.append(img);
  return box;
}

/* --- choosing a match by hand ---------------------------------------------
   Beets refuses whenever it cannot tell two releases apart, which for a
   popular record means five near-identical pressings and no winner. It knows
   perfectly well what the candidates are; `quiet_fallback: skip` throws the
   list away. This asks for the list back and lets a person point at one. */

const candidatesEl = document.getElementById("candidates");
// What the pending lookup was for. The answer arrives over the websocket,
// by which time nothing in the message says which row asked.
let candidatesFor = null;

function closeCandidates() {
  candidatesFor = null;
  candidatesEl.replaceChildren();
  candidatesEl.hidden = true;
}

function albumName(album) {
  return [album.artist, album.album].filter(Boolean).join(" - ")
    || album.folder || "this album";
}

function candidateRow(album, candidate) {
  const row = el("div", "candidate");
  const title = candidate.title || "(untitled)";
  const detail = [
    candidate.artist,
    candidate.year || null,
    candidate.tracks ? `${candidate.tracks} tracks` : null,
    candidate.album || null,
  ].filter(Boolean).join(" · ");

  const use = el("button", "ghost primary", "Use this");
  use.addEventListener("click", () => useCandidate(album, candidate, use));

  row.append(
    el("div", "candidate-title", title),
    el("div", "candidate-detail", detail),
    // Beets' own number. Lower is closer; it is shown because the gap
    // between the first and second is usually the whole story.
    el("div", "candidate-distance", candidate.distance.toFixed(2)),
    use
  );
  if (candidate.penalties && candidate.penalties.length) {
    row.append(el("div", "candidate-why",
                  `held against it: ${candidate.penalties.join(", ")}`));
  }
  return row;
}

function showCandidates(result) {
  const album = candidatesFor;
  if (!album) return;
  const head = el("div", "candidates-head");
  head.append(el("span", "candidates-title", `Matches for ${albumName(album)}`));
  const close = el("button", "ghost", "Close");
  close.addEventListener("click", closeCandidates);
  head.append(close);

  const nodes = [head];
  if (result.error) {
    nodes.push(el("p", "candidates-empty", result.error));
  } else if (!result.candidates.length) {
    nodes.push(el("p", "candidates-empty",
      "MusicBrainz has nothing close enough to offer. The album stays as it "
      + "is, tagged the way it arrived."));
  } else {
    nodes.push(...result.candidates.map((c) => candidateRow(album, c)));
  }
  candidatesEl.replaceChildren(...nodes);
  candidatesEl.hidden = false;
}

async function askForCandidates(album, button) {
  candidatesFor = album;
  button.disabled = true;
  candidatesEl.replaceChildren(
    el("p", "candidates-empty",
       `Asking MusicBrainz about ${albumName(album)}…`));
  candidatesEl.hidden = false;
  const payload = await startOperation(
    "candidates", "/api/library/match",
    { library_id: album.library_id, folder: album.folder });
  if (!payload || payload.detail) {
    closeCandidates();
    loadLibrary();
  }
}

async function useCandidate(album, candidate, button) {
  if (!confirm(
    `Tag "${albumName(album)}" as "${candidate.title}"`
    + `${candidate.artist ? ` by ${candidate.artist}` : ""}?\n\n`
    + "Every track in the folder is retagged. If the artist or album "
    + "changes they move to match, and the album keeps its identity — "
    + "stars and play counts survive.")) {
    return;
  }
  button.disabled = true;
  closeCandidates();
  await startOperation("import", "/api/library/match/apply",
                       { library_id: album.library_id, folder: album.folder,
                         release_id: candidate.id });
}

/* --- one album's tracks ---------------------------------------------------
   Fetched when a row is opened rather than with the listing: the listing
   counts tracks, and paying for every track in the library to show twelve
   of them is the cost that split exists to avoid. */

async function saveEdit(path, body, button, done) {
  button.disabled = true;
  const was = button.textContent;
  button.textContent = "Saving…";
  setNote("library-op", "");
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      setNote("library-op", data.detail
        || `That did not save (${response.status}).`, "warn");
      return false;
    }
    done(data);
    return true;
  } catch (err) {
    setNote("library-op", `Could not reach the server: ${err.message}`, "warn");
    return false;
  } finally {
    button.disabled = false;
    button.textContent = was;
  }
}

function field(label, value, extra = {}) {
  const wrap = el("label", "edit-field");
  wrap.append(el("span", "edit-label", label));
  const input = el("input");
  input.type = extra.number ? "number" : "text";
  input.value = value ?? "";
  if (extra.number) input.min = "0";
  if (extra.wide) input.classList.add("wide");
  wrap.append(input);
  wrap.input = input;
  return wrap;
}

// Renaming an album is one action over every file in it, because one album
// is one UUID - the artist and the title are properties of the folder, and
// editing them on a single track is how a record becomes two.
function albumEditor(album, reload) {
  const form = el("div", "album-edit");
  const artist = field("Album artist", album.artist, { wide: true });
  const name = field("Album", album.album, { wide: true });
  const save = el("button", "ghost primary", "Save album");

  save.addEventListener("click", async () => {
    const wantArtist = artist.input.value.trim();
    const wantAlbum = name.input.value.trim();
    if (!wantArtist || !wantAlbum) {
      setNote("library-op", "An artist and an album cannot be blank.", "warn");
      return;
    }
    if (wantArtist === album.artist && wantAlbum === album.album) return;
    if (!confirm(
      `Rename this album to "${wantArtist} — ${wantAlbum}"?\n\n`
      + `All ${album.tracks} track${album.tracks === 1 ? "" : "s"} are `
      + "retagged and the files move to match.\n"
      + "The album keeps its identity, so stars and play counts survive — "
      + "unless an album of that name already exists, in which case these "
      + "join it.")) return;

    await saveEdit("/api/library/album/edit", {
      library_id: album.library_id, folder: album.folder,
      album_artist: wantArtist, album: wantAlbum,
    }, save, (data) => {
      setNote("library-op",
              `Renamed, and ${data.moved} file${data.moved === 1 ? "" : "s"} `
              + "moved to match.", "notice");
      libraryOpen.delete(albumKey(album));
      reload();
    });
  });

  form.append(artist, name, save);
  return form;
}

// One track. Title, artist and the numbers rename it where it is; the move
// panel takes it out of this album and into another, which is how a
// misfiled track is rescued and how one gets split off by mistake.
function trackEditor(album, track, reload) {
  const form = el("div", "track-edit");
  const title = field("Title", track.title, { wide: true });
  const artist = field("Artist", track.artist, { wide: true });
  const number = field("Track", track.track_no || "", { number: true });
  const disc = field("Disc", track.disc_no || "", { number: true });
  const save = el("button", "ghost primary", "Save");

  const moveWrap = el("div", "track-move");
  moveWrap.hidden = true;
  const moveArtist = field("Album artist", album.artist, { wide: true });
  const moveAlbum = field("Album", album.album, { wide: true });
  const single = el("button", "ghost", "As its own single");
  const moveSave = el("button", "ghost primary", "Move this track");

  // A single is an album of one, which is how Spotify presents it and how
  // the filer files it. Without this every track rescued from Unknown Album
  // needs an album name invented for it by hand.
  single.addEventListener("click", () => {
    moveArtist.input.value = artist.input.value.trim()
      || track.artist || album.artist;
    moveAlbum.input.value = title.input.value.trim() || track.title;
  });

  const reveal = el("button", "ghost", "Move to another album…");
  reveal.addEventListener("click", () => {
    moveWrap.hidden = !moveWrap.hidden;
    reveal.textContent = moveWrap.hidden
      ? "Move to another album…" : "Cancel move";
  });

  save.addEventListener("click", async () => {
    const body = { library_id: album.library_id, path: track.path };
    if (title.input.value.trim() !== (track.title || "")) {
      body.title = title.input.value.trim();
    }
    if (artist.input.value.trim() !== (track.artist || "")) {
      body.artist = artist.input.value.trim();
    }
    const n = parseInt(number.input.value, 10);
    if (!Number.isNaN(n) && n !== track.track_no) body.track_no = n;
    const d = parseInt(disc.input.value, 10);
    if (!Number.isNaN(d) && d !== track.disc_no) body.disc_no = d;
    if (Object.keys(body).length <= 2) return;

    // Setting the artist of a track with no album artist changes which
    // album the file is on, so the server may move it. Say so first.
    if (body.artist !== undefined && !album.artist) {
      if (!confirm(
        `Set this track's artist to "${body.artist}"?\n\n`
        + "It has no album artist, so this also decides which folder it "
        + "lives in and the file will move.")) return;
    }
    await saveEdit("/api/library/track/edit", body, save, () => {
      setNote("library-op", "Saved.", "notice");
      libraryOpen.delete(albumKey(album));
      reload();
    });
  });

  moveSave.addEventListener("click", async () => {
    const wantArtist = moveArtist.input.value.trim();
    const wantAlbum = moveAlbum.input.value.trim();
    if (!wantArtist || !wantAlbum) {
      setNote("library-op", "An artist and an album cannot be blank.", "warn");
      return;
    }
    if (!confirm(
      `Move "${track.title}" to "${wantArtist} — ${wantAlbum}"?\n\n`
      + "Only this track moves. It leaves this album and joins that one, "
      + "taking its own stars and play count with it.\n"
      + "Everything else in this album stays where it is.")) return;

    await saveEdit("/api/library/track/edit", {
      library_id: album.library_id, path: track.path,
      album_artist: wantArtist, album: wantAlbum,
    }, moveSave, () => {
      setNote("library-op", `"${track.title}" moved.`, "notice");
      libraryOpen.delete(albumKey(album));
      reload();
    });
  });

  moveWrap.append(moveArtist, moveAlbum, single, moveSave);
  form.append(title, artist, number, disc, save, reveal, moveWrap);
  return form;
}

async function loadTracks(album, into) {
  const reload = () => loadLibrary();
  into.replaceChildren(el("div", "album-track", "Reading…"));
  try {
    const response = await fetch(
      `/api/library/album?library_id=${album.library_id}`
      + `&folder=${encodeURIComponent(album.folder)}`);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      into.replaceChildren(el("div", "album-track",
        data.detail || `Could not read that album (${response.status}).`));
      return;
    }

    const nodes = [albumEditor(album, reload)];
    for (const track of data.items) {
      const row = el("div", `album-track${track.tagged ? "" : " unmatched"}`);
      const editor = el("div", "track-editor");
      editor.hidden = true;
      const edit = el("button", "ghost", "Edit");
      edit.addEventListener("click", () => {
        if (!editor.childElementCount) {
          editor.append(trackEditor(album, track, reload));
        }
        editor.hidden = !editor.hidden;
        edit.textContent = editor.hidden ? "Edit" : "Done";
      });
      row.append(
        el("span", "track-no", track.track_no ? String(track.track_no) : "—"),
        cover(track.id, 32),
        el("span", "track-title", track.title || "(untitled)"),
        el("span", "track-meta", track.artist || ""),
        el("span", "track-meta dim",
           track.tagged ? "" : "no MusicBrainz match"),
        edit
      );
      nodes.push(row, editor);
    }
    into.replaceChildren(...nodes);
  } catch (err) {
    into.replaceChildren(el("div", "album-track",
                            `Could not reach the server: ${err.message}`));
  }
}

function albumRow(album) {
  // A wrapper with a clickable head and a body that is not. The toggle used
  // to sit on the whole row with the body inside it, so a click on an input
  // bubbled up and collapsed the album out from under whoever was typing -
  // which made the editor unusable rather than merely annoying.
  const row = el("div", "album-row");
  const head = el("div", "album-head");
  const actions = el("div", "staging-actions");
  const match = el("button", "ghost", "Find matches");
  match.addEventListener("click", (event) => {
    event.stopPropagation();
    askForCandidates(album, match);
  });
  actions.append(match);

  // Three states, not two. An album where a few tracks are unconfirmed is
  // usually a download that joined a matched record; one where none are is
  // a record nobody has looked at; and a matched one is simply done.
  let counted;
  if (album.matched) {
    counted = `${album.tracks} track${album.tracks === 1 ? "" : "s"}`;
  } else if (album.partial) {
    counted = `${album.untagged} of ${album.tracks} unmatched`;
  } else {
    counted = `${album.tracks} track${album.tracks === 1 ? "" : "s"}, `
            + "no MusicBrainz match";
  }

  const caret = el("span", "album-caret", "▸");
  head.append(
    caret,
    cover(album.art_id, 48),
    el("span", "staging-name", albumName(album)),
    el("span", "staging-meta", counted),
    el("span", "staging-kind", album.library || "library"),
    actions
  );
  head.title = album.folder;

  const body = el("div", "album-body");
  body.hidden = true;
  row.append(head, body);

  head.addEventListener("click", (event) => {
    if (event.target.closest("button")) return;
    const key = albumKey(album);
    if (body.hidden) {
      libraryOpen.add(key);
      body.hidden = false;
      caret.textContent = "▾";
      loadTracks(album, body);
    } else {
      libraryOpen.delete(key);
      body.hidden = true;
      caret.textContent = "▸";
    }
  });

  if (libraryOpen.has(albumKey(album))) {
    body.hidden = false;
    caret.textContent = "▾";
    loadTracks(album, body);
  }
  return row;
}

async function loadLibrary(append = false) {
  const offset = append ? libraryShownCount : 0;
  const query = new URLSearchParams({
    limit: String(LIBRARY_PAGE),
    offset: String(offset),
    unmatched: libraryUnmatched.checked ? "true" : "false",
    q: librarySearch.value.trim(),
  });
  try {
    const response = await fetch(`/api/library?${query}`);
    // fetch does not throw on 4xx or 5xx, and the body of an error is a
    // {detail} with no `available` key - which fell through to the empty
    // state and reported an empty library, the most alarming possible way
    // to be wrong.
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.available === false) {
      libraryEl.replaceChildren();
      libraryShownCount = 0;
      libraryCount.hidden = true;
      libraryMore.hidden = true;
      libraryEmpty.textContent =
        `Could not read your library: ${data.reason || data.detail
         || `the server answered ${response.status}`}`;
      libraryEmpty.hidden = false;
      return;
    }

    const albums = data.albums || [];
    const rows = albums.map(albumRow);
    if (append) {
      libraryEl.append(...rows);
      libraryShownCount += albums.length;
    } else {
      libraryEl.replaceChildren(...rows);
      libraryShownCount = albums.length;
    }

    // About the whole library, not the filtered page - so the numbers do not
    // move when the filter does.
    libraryCount.textContent =
      `${data.albums_total} album${data.albums_total === 1 ? "" : "s"}, `
      + `${data.tracks} track${data.tracks === 1 ? "" : "s"}. `
      + `${data.unmatched_albums} album`
      + `${data.unmatched_albums === 1 ? "" : "s"} have no MusicBrainz match.`;
    libraryCount.hidden = !data.tracks;

    const filtered = libraryUnmatched.checked || librarySearch.value.trim();
    libraryEmpty.textContent = libraryShownCount
      ? ""
      : filtered ? "Nothing matches that." : "Nothing in your library yet.";
    libraryEmpty.hidden = libraryShownCount > 0;
    // A page that came back short means there is no more, however the total
    // compares - the list can change under you while you read it.
    libraryMore.hidden = libraryShownCount >= data.total || !albums.length;
    // The badge counts what wants attention, not what exists.
    setBadge(libraryBadge, data.unmatched_albums);
  } catch (err) {
    libraryEmpty.textContent = `Could not read your library: ${err.message}`;
    libraryEmpty.hidden = false;
  }
}

libraryMore.addEventListener("click", () => {
  libraryMore.disabled = true;
  loadLibrary(true).finally(() => { libraryMore.disabled = false; });
});

libraryUnmatched.addEventListener("change", () => loadLibrary());

// Debounced: one request per pause, not one per keystroke. Each costs a walk
// of every row in Navidrome's index.
let librarySearchTimer = null;
librarySearch.addEventListener("input", () => {
  clearTimeout(librarySearchTimer);
  librarySearchTimer = setTimeout(() => loadLibrary(), 250);
});

// Import and audit are started, not awaited - beets gets 900s per path and
// an audit reads every file in the library, both far longer than a browser
// will hold a request open. The server pushes the outcome over the socket.
const OPERATION_LABELS = {
  // Applying a chosen match runs beets, which takes far longer than a
  // request should be held open. It has no button of its own - it is started
  // from a candidate row - so only the note is named here.
  import: { note: "library-op" },
  audit: { button: "health-audit", idle: "Re-read files", busy: "Reading…",
           note: "health-op" },
  // No button of its own: it is started from a row, and its result is a
  // list rather than a message.
  candidates: { note: "library-op" },
};

// An operation's outcome belongs to the panel that started it. The banner at
// the top of the page is outside every section, so anything left there
// followed you onto Queue, Browse, Playlists and Settings and stayed until
// something else happened to clear it - which for an import that reported
// "already running" was easily never.
function setNote(id, message, tone) {
  setBanner(document.getElementById(id), message, tone);
}

function importSummary(result) {
  const failed = result.failed || [];
  if (failed.length) return [`Retagging problems: ${failed.join("; ")}`, "warn"];
  if (result.imported) {
    return ["Retagged. It keeps the album identity it had, so nothing "
            + "starred was lost.", "notice"];
  }
  // Nothing changed and nothing failed: beets ran and refused. Said plainly,
  // because the album looking untouched is exactly how this used to hide.
  if (result.skipped) {
    return ["That release did not tag it; the album is unchanged.", "warn"];
  }
  return ["", "notice"];
}

function showOperation(operation) {
  const spec = OPERATION_LABELS[operation.name];
  if (!spec) return;
  const button = document.getElementById(spec.button);
  const running = operation.status === "running";
  if (button) {
    button.disabled = running;
    button.textContent = running ? spec.busy : spec.idle;
  }
  if (operation.name === "import") {
    // The per-row buttons run the same beets lock, so they cannot be live
    // while a retag is in flight. A finished one re-renders them.
    libraryEl.querySelectorAll("button").forEach((b) => { b.disabled = running; });
  }
  if (running) return;

  if (operation.status === "failed") {
    if (operation.name === "candidates") closeCandidates();
    setNote(spec.note, `${operation.name} failed: ${operation.error}`, "warn");
    return;
  }
  const result = operation.result || {};
  if (operation.name === "candidates") {
    showCandidates(result);
    // The album rows re-render with their buttons live again.
    loadLibrary();
    return;
  }
  if (operation.name === "import") {
    if (result.busy) {
      setNote(spec.note,
              "An import is already running; this one was not started.", "warn");
    } else if (!result.ran) {
      setNote(spec.note, `Nothing to import: ${result.reason}`, "warn");
    } else {
      const [message, tone] = importSummary(result);
      setNote(spec.note, message, tone);
    }
    loadLibrary();
  } else {
    setNote(spec.note, "");
    loadHealth();
  }
}

async function startOperation(name, path, body) {
  const spec = OPERATION_LABELS[name];
  setNote(spec.note, "");
  try {
    const request = { method: "POST" };
    if (body) {
      request.headers = { "Content-Type": "application/json" };
      request.body = JSON.stringify(body);
    }
    const payload = await fetch(path, request).then((r) => r.json());
    if (payload.detail) {
      setNote(spec.note, payload.detail, "warn");
      return payload;
    }
    if (!payload.started) {
      setNote(spec.note,
              "That is already running; watching the one in flight.", "warn");
    }
    showOperation(payload.operation);
    return payload;
  } catch (err) {
    setNote(spec.note, `Could not start: ${err.message}`, "warn");
    return null;
  }
}

// The list is read from Navidrome's database, which refreshes on scan, so it
// can be a few minutes behind the disk. That is fine for a page somebody
// opens deliberately and not fine when they have just fixed something.
document.getElementById("library-rescan").addEventListener("click", async () => {
  const button = document.getElementById("library-rescan");
  button.disabled = true;
  button.textContent = "Scanning…";
  try {
    const payload = await fetch("/api/library/rescan", { method: "POST" })
      .then((r) => r.json());
    setNote("library-op", payload.detail || "", payload.detail ? "warn" : "");
  } catch (err) {
    setNote("library-op", `Could not ask for a scan: ${err.message}`, "warn");
  } finally {
    button.disabled = false;
    button.textContent = "Rescan";
    loadLibrary();
  }
});

/* --- home ----------------------------------------------------------------
   The landing panel. It answers one question - what have you been listening
   to - because that is the question the thing this sits beside cannot
   answer at all: Navidrome keeps a cumulative total per track and the single
   most recent play date, so "how much did I listen to in March" has no home
   anywhere else.

   Everything drawn here is a door into a section that holds the real
   detail. Nothing on this page is a control, and nothing is edited here. */

const homeGreeting = document.getElementById("home-greeting");
const homeHero = document.getElementById("home-hero");
const homeStats = document.getElementById("home-stats");
const homeMonths = document.getElementById("home-months");
const homeArtists = document.getElementById("home-artists");
const homeSnapshots = document.getElementById("home-snapshots");
const homeEmpty = document.getElementById("home-empty");

const SVG_NS = "http://www.w3.org/2000/svg";

function svg(tag, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    node.setAttribute(key, String(value));
  }
  return node;
}

function greeting() {
  const hour = new Date().getHours();
  if (hour < 5) return "Still up";
  if (hour < 12) return "Good morning";
  if (hour < 18) return "Good afternoon";
  return "Good evening";
}

const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function monthLabel(month) {
  // Read off the string rather than through a Date: new Date("2026-09") is
  // UTC midnight, which reads as August everywhere west of Greenwich.
  return MONTH_NAMES[Number(String(month).slice(5, 7)) - 1] || "";
}

// The chart is drawn in its own coordinate space and scaled to fit by CSS,
// so these are ratios rather than pixels. Strokes opt out of the scaling.
const PLOT = { w: 720, h: 170, top: 14, bottom: 24, side: 6 };

function monthlyChart(months) {
  const wrap = el("div", "chart-wrap");
  if (!months.length) return wrap;

  const most = Math.max(1, ...months.map((m) => m.plays));
  const inner = PLOT.w - PLOT.side * 2;
  const floor = PLOT.h - PLOT.bottom;
  const step = months.length > 1 ? inner / (months.length - 1) : 0;
  const x = (i) => PLOT.side + step * i;
  const y = (plays) => floor - (plays / most) * (floor - PLOT.top);

  const chart = svg("svg", {
    viewBox: "0 0 " + PLOT.w + " " + PLOT.h,
    class: "chart", preserveAspectRatio: "none", role: "img",
    "aria-label": "Plays by month over " + months.length
      + " months, peaking at " + most,
  });

  // Hairline and recessive. Three lines, so the eye has something to
  // measure against without the grid becoming the picture.
  for (const fraction of [0, 0.5, 1]) {
    const at = floor - fraction * (floor - PLOT.top);
    chart.append(svg("line", {
      x1: PLOT.side, x2: PLOT.w - PLOT.side, y1: at, y2: at,
      class: fraction ? "chart-grid" : "chart-base",
    }));
  }

  const spine = months.map((m, i) => x(i) + "," + y(m.plays)).join(" ");
  chart.append(svg("polygon", {
    class: "chart-fill",
    points: PLOT.side + "," + floor + " " + spine + " "
            + x(months.length - 1) + "," + floor,
  }));
  chart.append(svg("polyline", { class: "chart-line", points: spine }));

  // The peak, marked where it happened. One mark rather than twenty-four
  // numbers: the shape carries the rest, and the hover layer has the exact
  // figure for any month somebody actually wants.
  // Placed over the chart rather than inside it: the plot is stretched to
  // the panel width, and a circle drawn in that coordinate space comes out
  // an ellipse at every width but one.
  const peak = months.findIndex((m) => m.plays === most);
  const dot = el("span", "chart-peak");
  dot.hidden = !(months[peak] && months[peak].plays > 0);
  dot.style.left = (x(peak) / PLOT.w) * 100 + "%";
  dot.style.top = (y(most) / PLOT.h) * 100 + "%";

  const crosshair = svg("line", {
    class: "chart-crosshair", x1: 0, x2: 0, y1: PLOT.top - 8, y2: floor,
  });
  crosshair.style.opacity = "0";
  chart.append(crosshair);

  const tip = el("div", "chart-tip");
  tip.hidden = true;

  // One hit target per month, the full height of the plot. The line itself
  // is 2px and nobody can hover 2px on a trackpad.
  months.forEach((month, i) => {
    const width = Math.max(1, step || inner);
    const band = svg("rect", {
      class: "chart-hit", y: 0, height: PLOT.h, tabindex: 0,
      x: Math.max(0, x(i) - width / 2), width,
    });
    const show = () => {
      crosshair.setAttribute("x1", x(i));
      crosshair.setAttribute("x2", x(i));
      crosshair.style.opacity = "1";
      tip.hidden = false;
      tip.textContent = monthLabel(month.month) + " "
        + String(month.month).slice(0, 4) + " · "
        + month.plays.toLocaleString()
        + (month.plays === 1 ? " play" : " plays");
      // Kept inside the box at both ends, or the first and last months
      // push their own tooltip off the edge of the panel.
      const across = x(i) / PLOT.w;
      tip.style.left = Math.min(0.92, Math.max(0.08, across)) * 100 + "%";
    };
    band.addEventListener("pointerenter", show);
    band.addEventListener("focus", show);
    chart.append(band);
  });

  const hide = () => {
    crosshair.style.opacity = "0";
    tip.hidden = true;
  };
  chart.addEventListener("pointerleave", hide);
  chart.addEventListener("focusout", hide);

  const axis = el("div", "chart-axis");
  // First, last, and each January between them: enough to place the shape
  // in time without twenty-four labels fighting over the same inch.
  months.forEach((month, i) => {
    const last = i === months.length - 1;
    const january = String(month.month).endsWith("-01");
    if (i !== 0 && !last && !january) return;
    const label = el("span", "chart-axis-label",
      january && !last ? String(month.month).slice(0, 4)
                       : monthLabel(month.month));
    label.style.left = (x(i) / PLOT.w) * 100 + "%";
    axis.append(label);
  });

  // Everything positioned as a percentage of the plot goes inside the plot
  // - the wrapper is taller by the height of the axis strip, and a dot
  // placed against that lands below the line it is meant to sit on.
  const plot = el("div", "chart-plot");
  plot.append(chart, dot,
              el("span", "chart-peak-label", most.toLocaleString()), tip);
  wrap.append(plot, axis);
  return wrap;
}

function artistBars(artists) {
  if (!artists.length) {
    return [el("p", "empty", "Nothing played in the last year yet.")];
  }
  const most = Math.max(1, ...artists.map((a) => a.plays));
  return artists.map((artist) => {
    const row = el("div", "home-bar-row");
    const track = el("div", "home-bar");
    const fill = el("div", "home-bar-fill");
    fill.style.width = Math.max(1.5, (artist.plays / most) * 100) + "%";
    track.append(fill);
    row.append(el("span", "home-bar-name", artist.artist), track,
               el("span", "home-bar-value", artist.plays.toLocaleString()));
    return row;
  });
}

function heroFact(facts) {
  homeHero.hidden = !facts.length;
  if (!facts.length) return;
  // Picked here rather than on the server, so the headline changes on every
  // visit without another request, and the server stays cacheable.
  const fact = facts[Math.floor(Math.random() * facts.length)];
  homeHero.querySelector(".home-hero-lead").textContent = fact.lead;
  homeHero.querySelector(".home-hero-value").textContent = fact.value;
  homeHero.querySelector(".home-hero-tail").textContent = fact.tail;
}

function homeTiles(data) {
  const heard = data.listening || {};
  const year = heard.year || {};
  const held = data.collection || {};
  const hours = Math.round((year.seconds || 0) / 3600);

  // Against last month rather than an average: it is the comparison anyone
  // makes on their own anyway, and the average of a series this short is
  // mostly noise.
  const change = (heard.this_month || 0) - (heard.last_month || 0);
  const versus = heard.last_month
    ? (change >= 0 ? "+" : "−") + Math.abs(change).toLocaleString()
      + " on last month"
    : "no month before this one";

  const tiles = [
    stat("This month", (heard.this_month || 0).toLocaleString(), versus),
    stat("In " + (year.year || new Date().getFullYear()),
         (year.tracks || 0).toLocaleString(),
         "different tracks, about " + hours.toLocaleString() + " hours"),
  ];
  if (held.available) {
    tiles.push(stat("Your library", (held.tracks || 0).toLocaleString(),
                    (held.albums || 0).toLocaleString() + " albums"));
  }
  homeStats.replaceChildren(...tiles);
}

async function loadHome() {
  try {
    const data = await fetch("/api/overview").then((r) => r.json());
    if (data.detail) {
      homeEmpty.hidden = false;
      homeEmpty.textContent = data.detail;
      return;
    }
    const heard = data.listening || {};
    homeEmpty.hidden = true;
    homeGreeting.textContent = greeting() + ", " + data.username + ".";
    heroFact(data.highlights || []);
    homeTiles(data);
    homeMonths.replaceChildren(monthlyChart(heard.months || []));
    homeArtists.replaceChildren(...artistBars(heard.top_artists || []));

    // Only when it is wrong. A tick saying the nightly job ran is noise on
    // a page whose job is to look calm; a job that stopped three weeks ago
    // is the one thing here worth interrupting for, because every day it
    // does not run is a day of listening nobody can recover.
    const snaps = data.snapshots || {};
    setBanner(homeSnapshots, snaps.up_to_date === false
      ? "No play snapshot since " + (snaps.last_run || "ever")
        + " — listening from " + snaps.awaiting
        + " onwards is not being recorded."
      : "", "warn");
  } catch (err) {
    homeEmpty.hidden = false;
    homeEmpty.textContent = "Could not load: " + err.message;
  }
}

homeHero.addEventListener("click", () => showView("listening"));

/* --- listening -----------------------------------------------------------
   The snapshots have been running since before there was anywhere to read
   them, which made four years of imported history and a nightly job look
   from the outside exactly like nothing happening at all. This is that
   record, and the first thing it shows is what has been captured - because
   the number that matters most is still "is this collecting or not". */

const listeningEl = document.getElementById("listening");
const listeningEmpty = document.getElementById("listening-empty");
const listeningError = document.getElementById("listening-error");
const listeningCoverage = document.getElementById("listening-coverage");
let listeningDays = 3650;

function stat(label, value, detail) {
  const box = el("div", "stat");
  box.append(el("div", "stat-label", label), el("div", "stat-value", value));
  if (detail) box.append(el("div", "stat-detail", detail));
  return box;
}

function coverage(cover, window) {
  const boxes = [stat(
    "Plays in view", window.plays.toLocaleString(),
    `${window.start} to ${window.end}`)];

  if (cover.imported_plays) {
    boxes.push(stat(
      "Imported history", cover.imported_plays.toLocaleString(),
      `${(cover.imported_sources || []).join(", ")}, from ${cover.imported_from}`));
  }
  boxes.push(stat(
    "Nightly snapshots", (cover.days_run || 0).toLocaleString(),
    cover.last_run ? `last ${cover.last_run}` : "none yet"));
  // The one that is a health question rather than a statistic: a nightly job
  // that quietly stopped looks exactly like one that ran and found nothing.
  boxes.push(stat(
    "Up to date", cover.up_to_date ? "yes" : "no",
    cover.up_to_date ? `through ${cover.awaiting}` : `waiting on ${cover.awaiting}`));

  listeningCoverage.replaceChildren(...boxes);
}

function listeningRow(track, rank, most) {
  const row = el("div", `listen-row${track.known ? "" : " gone"}`);
  const bar = el("div", "listen-bar");
  const fill = el("div", "listen-bar-fill");
  // Proportional to the top row, so the shape of the list is readable
  // without reading any of the numbers.
  fill.style.width = `${Math.max(2, Math.round(100 * track.plays / most))}%`;
  bar.append(fill);
  row.append(
    el("span", "listen-rank", String(rank)),
    el("span", "listen-title", track.title),
    el("span", "listen-artist", track.artist || ""),
    bar,
    el("span", "listen-plays", track.plays.toLocaleString())
  );
  if (!track.known) {
    row.title = "Played, but no longer in the library. The count is kept "
              + "against the track's identity, not its file.";
  }
  return row;
}

async function loadListening() {
  try {
    const data = await fetch(
      `/api/playcounts/top?days=${listeningDays}&limit=50`
    ).then((r) => r.json());
    if (data.detail) {
      setBanner(listeningError, data.detail, "warn");
      return;
    }
    setBanner(listeningError, "");
    const tracks = data.tracks || [];
    coverage(data.coverage || {}, data);

    const most = tracks.length ? tracks[0].plays : 1;
    listeningEl.replaceChildren(
      ...tracks.map((track, index) => listeningRow(track, index + 1, most)));
    listeningEmpty.textContent = tracks.length
      ? "" : "Nothing played in this window yet.";
    listeningEmpty.hidden = tracks.length > 0;
  } catch (err) {
    listeningEmpty.hidden = false;
    listeningEmpty.textContent = `Could not read play counts: ${err.message}`;
  }
}

document.querySelectorAll(".listen-range .range").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".listen-range .range")
      .forEach((other) => other.classList.toggle("active", other === button));
    listeningDays = Number(button.dataset.days);
    loadListening();
  });
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
  const row = el("div", `check ${check.status}${check.secondary ? " secondary" : ""}`);
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
  setBanner(healthError, report.navidrome_error
    ? `Navidrome database unreadable: ${report.navidrome_error}` : "", "warn");

  // Every row, always. The toggle hid the status rows behind a click that
  // had to be made on every visit to see the same panel, which is a worse
  // trade than a slightly longer list. `secondary` still dims a row and
  // still keeps it out of the badge: it says "this is status, not something
  // to act on", which is a different question from whether to show it.
  const blocks = [];
  report.sections.forEach((section) => {
    if (!section.checks.length) return;
    const block = el("section", "check-group");
    block.append(el("h2", "section-head", section.title));
    block.append(...section.checks.map(renderCheck));
    blocks.push(block);
  });

  healthEl.replaceChildren(...blocks);
  healthEmpty.hidden = blocks.length > 0;

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
    setBanner(dupeResult, "");
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
  setBanner(dupeResult, parts.join(" "), failed.length ? "warn" : "notice");
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

  setBanner(
    dupeNote,
    payload.confident
      ? `${payload.confident} group(s) share a MusicBrainz recording id and `
        + "can be resolved in one go."
      : "",
    "warn");
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
    setBanner(
      dupeResult,
      `Resolved ${result.resolved || 0} group(s).`
      + (failed.length
         ? ` ${failed.length} problem(s): ${failed.slice(0, 3).join("; ")}` : ""),
      failed.length ? "warn" : "notice");
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
  setBanner(signinError, "");
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
      setBanner(signinError,
                body.detail || `Sign in failed (${response.status})`, "error");
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
  setBanner(playlistError, message, "warn");
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
  // The panel the page opens on, so it is not blank until somebody
  // navigates away and back.
  loadHome();
  loadHealth();
  loadLibrary();
  checkSpotify();
  healthTimer = setInterval(loadHealth, 5 * 60 * 1000);
}

checkSession().then((signedIn) => {
  if (signedIn) start();
});
