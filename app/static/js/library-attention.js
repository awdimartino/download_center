"use strict";

/* --- library: needs attention ---------------------------------------------
   What wants a person, grouped by why: singles that belong together, covers
   with bars, albums nobody has reviewed or measured. Also the bulk actions
   select mode offers, and ReplayGain, which is started from here and from
   an album's panel. */

import { el, setNote } from "./core.js";
import { registerOperation, startOperation } from "./operations.js";
import {
  actionButton,
  albumArt,
  albumKey,
  artStamps,
  getJSON,
  libraryCount,
  libraryEl,
  libraryEmpty,
  libraryMore,
  librarySearch,
  libraryState,
  librarySuggest,
  libraryTodoCount,
  loadCoverSurvey,
  plural,
  postJSON,
  remember,
  showProgress,
  viewing
} from "./library-shared.js";
import {
  clickable,
  loadLibrary,
  refreshLibrary,
  refreshPicks
} from "./library.js";
import { closeDrawer } from "./library-drawer.js";
import { combineGroup } from "./library-combine.js";

// Groups somebody has said do not belong together. Keyed on exactly which
// singles were offered, so a new one arriving offers the group again.
function groupKey(group) {
  return [group.library_id, group.artist.toLowerCase(),
          ...group.albums.map((a) => a.folder).sort()].join("|");
}

function dismissed() {
  try {
    return new Set(JSON.parse(localStorage.getItem("library.dismissed") || "[]"));
  } catch {
    return new Set();
  }
}

function dismiss(group) {
  const set = dismissed();
  set.add(groupKey(group));
  remember("dismissed", JSON.stringify([...set]));
}

export function suggestedGroups() {
  if (!libraryState.attention) return [];
  const gone = dismissed();
  return libraryState.attention.together.filter((g) => !gone.has(groupKey(g)));
}

async function fetchAttention() {
  if (!libraryState.attention) libraryState.attention = await getJSON("/api/library/attention");
  updateTodoCount();
  return libraryState.attention;
}

export function updateTodoCount() {
  if (!libraryState.attention) return;
  const count = suggestedGroups().length + libraryState.attention.beside.length
    + (libraryState.coverSurvey ? libraryState.coverSurvey.count : 0);
  libraryTodoCount.textContent = count ? count.toLocaleString() : "";
  libraryTodoCount.hidden = !count;
}

function stackOf(albums) {
  const box = el("span", "lib-stack");
  box.append(...albums.slice(0, 4).map((a) => albumArt(a, 96)));
  return box;
}

export function suggestionBanner(group) {
  const banner = el("div", "lib-suggest");
  const text = el("span", "lib-suggest-text",
    `${plural(group.albums.length, "single")} by ${group.artist} look like one album`);
  text.append(el("small", "", group.albums.map((a) => a.album).join(", ")));

  const go = el("button", "", "Combine…");
  go.type = "button";
  go.addEventListener("click", () => combineGroup(group));
  const no = el("button", "ghost", "Not together");
  no.type = "button";
  no.addEventListener("click", () => {
    dismiss(group);
    banner.remove();
    updateTodoCount();
  });
  banner.append(stackOf(group.albums), text, go, no);
  return banner;
}

// On Albums, not narrowed: what arrived lately is what is most likely to be
// in pieces, so the suggestion sits at the top of what was just downloaded.
export async function showSuggestions() {
  const quiet = viewing.tab !== "albums" || viewing.show !== "all" || viewing.kind === "album"
    || librarySearch.value.trim();
  if (quiet) {
    librarySuggest.replaceChildren();
    return;
  }
  try {
    await fetchAttention();
  } catch {
    return;
  }
  librarySuggest.replaceChildren(...suggestedGroups().slice(0, 2).map(suggestionBanner));
}

function miniCard(album, sub) {
  const card = el("div", "lib-mini");
  card.append(albumArt(album, 150), el("span", "lib-card-title", album.album),
              el("span", "lib-card-sub", sub || album.artist));
  return clickable(card, album);
}

// Sections the person has folded away, by title. One long section - forty
// groups of singles - used to push everything below it off the screen, with
// no way past it but scrolling.
function collapsed() {
  try {
    return new Set(JSON.parse(localStorage.getItem("library.collapsed") || "[]"));
  } catch {
    return new Set();
  }
}

function setCollapsed(title, closed) {
  const set = collapsed();
  if (closed) set.add(title);
  else set.delete(title);
  remember("collapsed", JSON.stringify([...set]));
}

let sectionIds = 0;

// The frame every section shares: a heading that folds the section, the
// reason it exists, and a body for its contents. The actions stay in the
// heading when folded, so "Measure all" is still a click away.
function sectionShell({ title, count, why, actions = [] }) {
  const section = el("section", "lib-todo");
  const head = el("div", "lib-todo-head");
  const text = el("div", "lib-todo-text");
  const body = el("div", "lib-todo-body");
  body.id = `lib-todo-${++sectionIds}`;
  const toggle = el("button", "lib-todo-toggle");
  toggle.type = "button";
  toggle.setAttribute("aria-controls", body.id);
  toggle.append(el("span", "lib-todo-chevron", "▾"), el("span", "", title));
  if (count !== undefined) toggle.append(el("small", "", ` ${count.toLocaleString()}`));
  const h = el("h3");
  h.append(toggle);
  const reason = el("p", "", why);
  text.append(h, reason);
  head.append(text);
  if (count === 0) head.append(el("span", "lib-done", "✓ All clear"));
  else head.append(...actions);
  section.append(head, body);

  const show = (open) => {
    section.classList.toggle("collapsed", !open);
    toggle.setAttribute("aria-expanded", String(open));
    body.hidden = !open;
    reason.hidden = !open;
  };
  show(!collapsed().has(title));
  toggle.addEventListener("click", () => {
    const open = toggle.getAttribute("aria-expanded") !== "true";
    setCollapsed(title, !open);
    show(open);
  });
  return { section, body };
}

function todoSection({ title, count, why, albums, actions = [], sub }) {
  const { section, body } = sectionShell({ title, count, why, actions });
  if (albums && albums.length) {
    const strip = el("div", "lib-strip");
    strip.append(...albums.map((a) => miniCard(a, sub && sub(a))));
    body.append(strip);
  }
  return section;
}

export async function loadAttention() {
  librarySuggest.replaceChildren();
  libraryMore.hidden = true;
  libraryCount.hidden = true;
  libraryEmpty.hidden = true;
  if (!libraryState.attention) libraryEl.replaceChildren(el("p", "empty", "Reading…"));
  let data;
  try {
    data = await fetchAttention();
  } catch (err) {
    libraryEl.replaceChildren(el("p", "empty", `Could not read your library: ${err.message}`));
    return;
  }

  const groups = suggestedGroups();
  const { section: together, body: groupList } = sectionShell({
    title: "Singles that belong together",
    count: groups.length,
    why: "Songs downloaded one at a time arrive as an album each. "
      + "Two or more by one artist are offered here to combine.",
  });
  for (const group of groups) {
    const row = el("div", "lib-group");
    const words = el("span", "lib-suggest-text", group.albums.map((a) => a.album).join(", "));
    words.append(el("small", "", `${group.artist} · ${plural(group.albums.length, "single")}`));
    row.append(stackOf(group.albums), words,
      actionButton("Combine…", "", () => combineGroup(group)),
      actionButton("Not together", "ghost", () => {
        dismiss(group);
        loadAttention();
      }));
    groupList.append(row);
  }

  const covers = el("div");
  const nodes = [
    together,
    covers,
    todoSection({
      title: "Singles already on an album",
      count: data.beside.length,
      why: "The same song is on an album of that artist's you have. These are "
        + "duplicates rather than pieces of a record; open one to set it aside.",
      albums: data.beside,
      sub: (a) => `Also on ${a.on_album}`,
    }),
    todoSection({
      title: "Needs review",
      count: data.review.count,
      why: "Everything that arrives starts here. An album leaves when it gets a "
        + "MusicBrainz match, or when you mark it reviewed - some music is simply "
        + "not in MusicBrainz, and that is fine.",
      albums: data.review.albums,
      actions: [actionButton("See all", "ghost", () => setShow("review"))],
    }),
    todoSection({
      title: "No ReplayGain",
      count: data.no_gain.count,
      why: "These play louder or quieter than everything else until measured.",
      albums: data.no_gain.albums,
      actions: [
        actionButton("See all", "ghost", () => setShow("nogain")),
        actionButton("Measure all", "", () => measureAll()),
      ],
    }),
  ];
  libraryEl.replaceChildren(...nodes);
  refreshPicks();
  showCoverSurvey(covers);
}

// Last, and by itself: it opens a file per album, which on the Pi is seconds
// the first time. The other sections are already readable by then.
async function showCoverSurvey(into) {
  if (!libraryState.coverSurvey) {
    into.replaceChildren(todoSection({
      title: "Covers with bars",
      why: "Checking every album's cover…",
    }));
    try {
      await loadCoverSurvey();
    } catch (err) {
      into.replaceChildren(todoSection({
        title: "Covers with bars", why: `Could not check: ${err.message}`,
      }));
      return;
    }
  }
  updateTodoCount();
  if (!into.isConnected) return;
  into.replaceChildren(todoSection({
    title: "Covers with bars",
    count: libraryState.coverSurvey.count,
    why: "A YouTube video frame instead of album art. Squaring keeps the "
      + "picture and drops the bars, and changes nothing else.",
    albums: libraryState.coverSurvey.albums.slice(0, 24),
    actions: [actionButton("Square all", "", (b) =>
      squareCovers(libraryState.coverSurvey.albums, b))],
  }));
}

function setShow(show) {
  closeDrawer();
  viewing.tab = "albums";
  viewing.artist = null;
  viewing.show = show;
  viewing.kind = "all";
  loadLibrary();
  window.scrollTo(0, 0);
}

function measureAll() {
  if (!confirm(
    "Measure ReplayGain for every album that has none?\n\n"
    + "Each album is measured as a whole, so album gain stays consistent, and "
    + "its files are rewritten with the new tags. On the Pi this can take a "
    + "long while; it can be stopped between albums.")) return;
  startOperation("replaygain", "/api/library/replaygain", {});
}

/* --- bulk actions -------------------------------------------------------- */

// One album at a time, so a failure on one is reported and the rest still
// happen - and so the Pi is never asked to rewrite a hundred albums at once.
async function eachAlbum(albums, button, verb, work) {
  button.disabled = true;
  const was = button.textContent;
  let done = 0;
  const failed = [];
  for (const album of albums) {
    button.textContent = `${verb} ${done + 1} of ${albums.length}…`;
    try {
      await work(album);
      done += 1;
    } catch (err) {
      failed.push(`${album.album}: ${err.message}`);
    }
  }
  button.textContent = was;
  button.disabled = false;
  return { done, failed };
}

export async function squareCovers(albums, button) {
  let changed = 0;
  const { failed } = await eachAlbum(albums, button, "Squaring", async (album) => {
    const result = await postJSON("/api/library/cover/apply",
      { library_id: album.library_id, folder: album.folder, url: null });
    if (result.written || result.already_square) {
      libraryState.barredKeys.delete(albumKey(album));
      album.barred = false;
    }
    if (result.written) {
      changed += 1;
      artStamps.set(albumKey(album), Date.now());
    }
  });
  setNote("library-op",
    `Squared ${plural(changed, "cover")}.`
    + (failed.length ? ` Could not: ${failed.join("; ")}` : "")
    + (changed ? " Navidrome shows them after its rescan." : ""),
    failed.length ? "warn" : "notice");
  libraryState.coverSurvey = null;
  refreshLibrary();
}

export async function markReviewed(albums, button) {
  const { done, failed } = await eachAlbum(albums, button, "Marking", (album) =>
    postJSON("/api/library/reviewed",
             { library_id: album.library_id, folder: album.folder, reviewed: true }));
  setNote("library-op",
    `Marked ${plural(done, "album")} reviewed.`
    + (failed.length ? ` Could not: ${failed.join("; ")}` : ""),
    failed.length ? "warn" : "notice");
  refreshLibrary();
}

function gainSummary(result) {
  const parts = [`ReplayGain measured for ${plural(result.measured, "album")}`
                 + (result.total > 1 ? ` of ${result.total}` : "") + "."];
  if (result.stopped) parts.push("Stopped on request.");
  if (result.failures) {
    parts.push(`${result.failures} failed: ${result.failed.join("; ")}`);
  }
  if (result.skipped && result.skipped.length) {
    parts.push(`Skipped: ${result.skipped.join("; ")}`);
  }
  // Navidrome shows the new values after the scan it has been asked for.
  if (result.measured) parts.push("Navidrome picks it up on its next scan.");
  return [parts.join(" "), result.failures ? "warn" : "notice"];
}

registerOperation("replaygain", {
  note: "library-op",
  onButtonState(running, operation) {
    showProgress(operation, { label: "ReplayGain", ordinal: true,
                             stopUrl: "/api/library/replaygain/stop" });
  },
  onResult(result) {
    const [message, tone] = gainSummary(result);
    setNote("library-op", message, tone);
    refreshLibrary();
  },
});
