"use strict";

/* --- library ----------------------------------------------------------------
   Every album you own, one row per folder, newest first. The filter narrows
   to albums MusicBrainz has never confirmed - which is derived from the
   files every time, never stored, so nothing here can fall out of step with
   what is true.

   It is a filter and not the list itself, because hiding matched albums is
   what made one unreachable: applying the wrong release gave it MusicBrainz
   ids, dropped it off the only list carrying the button, and left no way to
   correct it. */

import { el, setNote } from "./core.js";
import { barRows } from "./charts.js";
import { setBadge } from "./nav.js";
import { registerOperation, startOperation } from "./operations.js";

const libraryEl = document.getElementById("library");
const libraryEmpty = document.getElementById("library-empty");
const libraryBadge = document.getElementById("library-badge");
const libraryCount = document.getElementById("library-count");
const libraryMore = document.getElementById("library-more");
const librarySearch = document.getElementById("library-search");
const libraryShow = document.getElementById("library-show");
const libraryGainAll = document.getElementById("library-gain-all");
const libraryProgress = document.getElementById("library-progress");
const libraryGenresToggle = document.getElementById("library-genres-toggle");
const libraryGenresSection = document.getElementById("library-genres");

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
function libraryArt(trackId, size) {
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
    refreshLibrary(album);
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

// Reports a quarantine outcome the same way whether it moved one track or a
// whole album, so the two callers below cannot drift in wording.
function reportQuarantine(data) {
  const moved = data.quarantined || [];
  const failed = data.failed || [];
  const parts = [];
  if (moved.length) {
    parts.push(`Set aside ${plural(moved.length, "file")} to duplicates-removed/.`);
  }
  if (failed.length) parts.push(`Could not move: ${failed.join("; ")}`);
  setNote("library-op", parts.join(" ") || "Nothing was moved.",
         failed.length ? "warn" : "notice");
}

// Set aside every file in an album folder by hand - the wrong record
// entirely, not a worse copy of a right one.
async function quarantineAlbum(album, button) {
  if (!confirm(
    `Move ${plural(album.tracks, "file")} to duplicates-removed/ inside `
    + `${album.library || "this library"}?\n\n${albumName(album)}\n\n`
    + "This is for the wrong record entirely, not a worse copy of a right "
    + "one. It cannot be undone from here.")) return;

  button.disabled = true;
  try {
    const response = await fetch("/api/library/quarantine", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        library_id: album.library_id, folder: album.folder,
        album: album.album, artist: album.artist,
      }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      setNote("library-op", data.detail || `That did not work (${response.status}).`, "warn");
      button.disabled = false;
      return;
    }
    reportQuarantine(data);
    libraryOpen.delete(albumKey(album));
    refreshLibrary();
  } catch (err) {
    setNote("library-op", `Could not reach the server: ${err.message}`, "warn");
    button.disabled = false;
  }
}

// Set aside one track by hand, leaving the rest of the album alone.
async function quarantineTrack(album, track, button, reload) {
  if (!confirm(
    `Move "${track.title}" to duplicates-removed/ inside `
    + `${album.library || "this library"}?\n\n${track.path}\n\n`
    + "This is for the wrong file entirely, not a worse copy of a right "
    + "one. It cannot be undone from here.")) return;

  button.disabled = true;
  try {
    const response = await fetch("/api/library/track/quarantine", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        library_id: album.library_id, folder: album.folder,
        track_id: track.id, album: album.album, artist: album.artist,
      }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      setNote("library-op", data.detail || `That did not work (${response.status}).`, "warn");
      button.disabled = false;
      return;
    }
    reportQuarantine(data);
    libraryOpen.delete(albumKey(album));
    reload();
  } catch (err) {
    setNote("library-op", `Could not reach the server: ${err.message}`, "warn");
    button.disabled = false;
  }
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

  // Retagging to an album name that already exists is what merges into it,
  // but that meant remembering and retyping an existing artist and title
  // exactly right - one typo made a new album instead of joining the one you
  // meant. This searches the whole library and fills the two fields above
  // from a real match, so a merge starts from a name that is known to exist.
  const mergeLabel = el("span", "edit-label", "Merge into an existing album");
  const mergeInput = el("input", "album-merge-input");
  mergeInput.type = "search";
  mergeInput.autocomplete = "off";
  mergeInput.placeholder = "Search artist or album to merge into";
  const mergeResults = el("div", "album-merge-results");
  mergeResults.hidden = true;

  async function searchMergeTargets(query) {
    let data;
    try {
      const response = await fetch(
        `/api/library?q=${encodeURIComponent(query)}&limit=8`);
      data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.detail || `HTTP ${response.status}`);
      }
    } catch (err) {
      mergeResults.replaceChildren(
        el("p", "album-merge-empty", `Could not search: ${err.message}`));
      mergeResults.hidden = false;
      return;
    }
    const matches = (data.albums || []).filter((a) => !(
      a.library_id === album.library_id && a.folder === album.folder));
    if (!matches.length) {
      mergeResults.replaceChildren(
        el("p", "album-merge-empty", "No other album matches."));
      mergeResults.hidden = false;
      return;
    }
    mergeResults.replaceChildren(...matches.map((a) => {
      const row = el("button", "album-merge-result");
      row.append(
        el("span", "album-merge-name", albumName(a)),
        el("span", "album-merge-count", plural(a.tracks, "track")));
      row.addEventListener("click", () => {
        artist.input.value = a.artist;
        name.input.value = a.album;
        mergeInput.value = "";
        mergeResults.hidden = true;
        mergeResults.replaceChildren();
      });
      return row;
    }));
    mergeResults.hidden = false;
  }

  // Debounced: one request per pause, not one per keystroke.
  let mergeTimer = null;
  mergeInput.addEventListener("input", () => {
    clearTimeout(mergeTimer);
    const query = mergeInput.value.trim();
    if (query.length < 2) {
      mergeResults.hidden = true;
      mergeResults.replaceChildren();
      return;
    }
    mergeTimer = setTimeout(() => searchMergeTargets(query), 250);
  });

  const merge = el("div", "album-merge");
  merge.append(mergeLabel, mergeInput, mergeResults);

  form.append(artist, name, save, merge);
  return form;
}

// The rarer edits for one track: its disc number, and moving it out of this
// album into another entirely, which is how a misfiled track is rescued and
// how one gets split off by mistake. Kept behind "More" so it cannot be
// triggered by the same accidental tap that would edit the title.
function trackMorePanel(album, track, titleInput, artistInput, reload) {
  const form = el("div", "track-edit");
  const disc = field("Disc", track.disc_no || "", { number: true });
  const discSave = el("button", "ghost primary", "Save disc");

  discSave.addEventListener("click", async () => {
    const d = parseInt(disc.input.value, 10);
    if (Number.isNaN(d) || d === (track.disc_no || 0)) return;
    await saveEdit("/api/library/track/edit",
      { library_id: album.library_id, path: track.path, disc_no: d },
      discSave, () => {
        setNote("library-op", "Saved.", "notice");
        libraryOpen.delete(albumKey(album));
        reload();
      });
  });

  const moveArtist = field("Album artist", album.artist, { wide: true });
  const moveAlbum = field("Album", album.album, { wide: true });
  const single = el("button", "ghost", "As its own single");
  const moveSave = el("button", "ghost primary", "Move this track");

  // A single is an album of one, which is how Spotify presents it and how
  // the filer files it. Without this every track rescued from Unknown Album
  // needs an album name invented for it by hand. Reads the title/artist
  // fields on the row itself, in case they have not been saved yet.
  single.addEventListener("click", () => {
    moveArtist.input.value = artistInput.value.trim()
      || track.artist || album.artist;
    moveAlbum.input.value = titleInput.value.trim() || track.title;
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

  const moveWrap = el("div", "track-move");
  moveWrap.append(moveArtist, moveAlbum, single, moveSave);

  const quarantine = el("button", "ghost primary", "Quarantine this track");
  quarantine.addEventListener("click", () => quarantineTrack(album, track, quarantine, reload));

  form.append(disc, discSave, moveWrap, quarantine);
  return form;
}

// One track, edited in its row: track number, title and artist save as soon
// as they lose focus with a changed value, with no separate edit mode to
// step into first and no Save button to find. Disc number and moving to
// another album are rarer, so they stay one tap away behind "More".
function trackRow(album, track, reload) {
  const row = el("div", `album-track${track.tagged ? "" : " unmatched"}`);

  const no = el("input", "track-no-input");
  no.type = "number";
  no.min = "0";
  no.value = track.track_no || "";
  no.setAttribute("aria-label", "Track number");

  const title = el("input", "track-title-input");
  title.type = "text";
  title.value = track.title || "";
  title.setAttribute("aria-label", "Title");

  const artist = el("input", "track-artist-input");
  artist.type = "text";
  artist.value = track.artist || "";
  artist.setAttribute("aria-label", "Artist");

  async function saveField(input, key, value, previous) {
    if (value === previous) return;
    // Setting the artist of a track with no album artist changes which
    // album the file is on, so the server may move it. Say so first.
    if (key === "artist" && !album.artist) {
      if (!confirm(
        `Set this track's artist to "${value}"?\n\n`
        + "It has no album artist, so this also decides which folder it "
        + "lives in and the file will move.")) {
        input.value = previous;
        return;
      }
    }
    await saveEdit("/api/library/track/edit",
      { library_id: album.library_id, path: track.path, [key]: value },
      input, () => {
        setNote("library-op", "Saved.", "notice");
        // Closed rather than re-read: Navidrome has not rescanned yet, and
        // the tracks it would list are the ones from before the save.
        libraryOpen.delete(albumKey(album));
        reload();
      });
  }

  no.addEventListener("change", () => {
    const n = parseInt(no.value, 10);
    if (Number.isNaN(n)) { no.value = track.track_no || ""; return; }
    saveField(no, "track_no", n, track.track_no || 0);
  });
  title.addEventListener("change", () => {
    saveField(title, "title", title.value.trim(), track.title || "");
  });
  artist.addEventListener("change", () => {
    saveField(artist, "artist", artist.value.trim(), track.artist || "");
  });

  const more = el("button", "ghost", "More…");
  const moreWrap = el("div", "track-editor");
  moreWrap.hidden = true;
  more.addEventListener("click", () => {
    if (!moreWrap.childElementCount) {
      moreWrap.append(trackMorePanel(album, track, title, artist, reload));
    }
    moreWrap.hidden = !moreWrap.hidden;
    more.textContent = moreWrap.hidden ? "More…" : "Less";
  });

  row.append(
    no,
    libraryArt(track.id, 32),
    title,
    artist,
    el("span", "track-meta dim", track.tagged ? "" : "no MusicBrainz match"),
    more
  );
  return { row, more: moreWrap };
}

async function loadTracks(album, into) {
  const reload = () => refreshLibrary(album);
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
      const { row, more } = trackRow(album, track, reload);
      nodes.push(row, more);
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
  row.dataset.key = albumKey(album);
  const head = el("div", "album-head");
  const actions = el("div", "album-actions");
  const match = el("button", "ghost", "Find matches");
  match.addEventListener("click", (event) => {
    event.stopPropagation();
    askForCandidates(album, match);
  });
  actions.append(match);

  // Only offered where there is something to decide: a matched album is
  // done by definition. Undoable, because a tap on the wrong row should not
  // lose an album from the list for good.
  if (!album.matched) {
    const review = el("button", "ghost",
                      album.reviewed ? "Needs review" : "Mark reviewed");
    review.title = album.reviewed
      ? "Put this album back on the review list"
      : "Tagged the way you want it, MusicBrainz or not";
    review.addEventListener("click", async (event) => {
      event.stopPropagation();
      await saveEdit("/api/library/reviewed", {
        library_id: album.library_id, folder: album.folder,
        reviewed: !album.reviewed,
      }, review, () => refreshLibrary(album));
    });
    actions.append(review);
  }

  const quarantine = el("button", "ghost primary", "Quarantine");
  quarantine.title = "Set this whole album aside - the wrong record "
    + "entirely, not a worse copy of a right one.";
  quarantine.addEventListener("click", (event) => {
    event.stopPropagation();
    quarantineAlbum(album, quarantine);
  });
  actions.append(quarantine);

  if (album.no_gain) {
    const gain = el("button", "ghost", "ReplayGain");
    gain.title = `${plural(album.no_gain, "track")} with no ReplayGain. `
               + "The whole album is measured, so album gain stays consistent.";
    gain.addEventListener("click", (event) => {
      event.stopPropagation();
      startOperation("replaygain", "/api/library/replaygain",
                     { library_id: album.library_id, folder: album.folder });
    });
    actions.append(gain);
  }

  // Three states, not two. An album where a few tracks are unconfirmed is
  // usually a download that joined a matched record; one where none are is
  // a record nobody has looked at; and a matched one is simply done.
  let counted;
  if (album.matched) {
    counted = plural(album.tracks, "track");
  } else if (album.partial) {
    counted = `${album.untagged} of ${album.tracks} unmatched`;
  } else {
    counted = `${plural(album.tracks, "track")}, no MusicBrainz match`;
  }
  if (album.reviewed) counted += " · reviewed";
  if (album.no_gain) counted += " · no ReplayGain";

  const caret = el("span", "album-caret", "▸");
  head.append(
    caret,
    libraryArt(album.art_id, 48),
    el("span", "album-name", albumName(album)),
    el("span", "album-meta", counted),
    el("span", "album-library", album.library || "library"),
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

// The server's own cap on one page. Refreshing more than this many rows
// takes several requests.
const LIBRARY_MAX_PAGE = 200;

function plural(n, word) {
  return `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
}

async function fetchLibraryPage(offset, limit) {
  const query = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
    show: libraryShow.value,
    q: librarySearch.value.trim(),
  });
  const response = await fetch(`/api/library?${query}`);
  // fetch does not throw on 4xx or 5xx, and the body of an error is a
  // {detail} with no `available` key - which fell through to the empty
  // state and reported an empty library, the most alarming possible way to
  // be wrong.
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.available === false) {
    throw new Error(data.reason || data.detail
                    || `the server answered ${response.status}`);
  }
  return data;
}

// Three ways to load, because they differ in what they keep:
//   "reset"   - a new filter or search: the first page, from the top.
//   "more"    - the next page, appended.
//   "refresh" - after something changed a row: every row already on screen
//               read again, and the page kept where it was.
// Every edit used to reload with "reset", so fixing album 180 put you back
// at album 50 with "Show more" under your thumb - and tapping it re-read the
// page you had just been on.
export async function loadLibrary(mode = "reset", anchor = null) {
  const keepScroll = mode === "refresh";
  // Where the row being worked on sits on screen, so it can be put back in
  // the same place however much the rows above it changed.
  const anchorRow = anchor
    ? libraryEl.querySelector(`[data-key="${CSS.escape(anchor)}"]`) : null;
  const anchorTop = anchorRow ? anchorRow.getBoundingClientRect().top : null;
  const scrollY = window.scrollY;

  try {
    let albums = [];
    let data;
    if (mode === "more") {
      data = await fetchLibraryPage(libraryShownCount, LIBRARY_PAGE);
      albums = data.albums || [];
    } else {
      const want = keepScroll ? Math.max(libraryShownCount, LIBRARY_PAGE) : LIBRARY_PAGE;
      do {
        data = await fetchLibraryPage(albums.length,
                                      Math.min(want - albums.length, LIBRARY_MAX_PAGE));
        albums = albums.concat(data.albums || []);
      } while (albums.length < want && albums.length < data.total
               && (data.albums || []).length);
    }

    const rows = albums.map(albumRow);
    if (mode === "more") {
      libraryEl.append(...rows);
      libraryShownCount += albums.length;
    } else {
      libraryEl.replaceChildren(...rows);
      libraryShownCount = albums.length;
    }

    if (keepScroll) {
      const again = anchor
        ? libraryEl.querySelector(`[data-key="${CSS.escape(anchor)}"]`) : null;
      if (again && anchorTop !== null) {
        window.scrollBy(0, again.getBoundingClientRect().top - anchorTop);
      } else {
        window.scrollTo(0, scrollY);
      }
    }

    // About the whole library, not the filtered page - so the numbers do not
    // move when the filter does.
    const parts = [
      `${plural(data.albums_total, "album")}, ${plural(data.tracks, "track")}.`,
      `${plural(data.review_albums, "album")} need${data.review_albums === 1 ? "s" : ""} review;`,
      `${plural(data.unmatched_albums, "album")} with no MusicBrainz match.`,
    ];
    if (data.no_gain) {
      parts.push(`${plural(data.no_gain, "track")} with no ReplayGain.`);
    }
    libraryCount.textContent = parts.join(" ");
    libraryCount.hidden = !data.tracks;

    libraryGainAll.hidden = !data.no_gain_albums;
    libraryGainAll.textContent =
      `Measure ReplayGain for ${plural(data.no_gain_albums, "album")}`;

    const filtered = libraryShow.value !== "all" || librarySearch.value.trim();
    libraryEmpty.textContent = libraryShownCount
      ? ""
      : filtered ? "Nothing matches that." : "Nothing in your library yet.";
    libraryEmpty.hidden = libraryShownCount > 0;
    // A page that came back short means there is no more, however the total
    // compares - the list can change under you while you read it.
    libraryMore.hidden = libraryShownCount >= data.total || !albums.length;
    // The badge counts what wants attention, not what exists - and "needs
    // review" is the count that can reach zero.
    setBadge(libraryBadge, data.review_albums);
  } catch (err) {
    if (mode !== "more") {
      libraryEl.replaceChildren();
      libraryShownCount = 0;
      libraryCount.hidden = true;
      libraryMore.hidden = true;
    }
    libraryEmpty.textContent = `Could not read your library: ${err.message}`;
    libraryEmpty.hidden = false;
  }
}

// After a change to one album: re-read what is on screen, keep that album
// where it was.
export function refreshLibrary(album) {
  return loadLibrary("refresh", album ? albumKey(album) : null);
}

libraryMore.addEventListener("click", () => {
  libraryMore.disabled = true;
  loadLibrary("more").finally(() => { libraryMore.disabled = false; });
});

libraryShow.addEventListener("change", () => loadLibrary());

libraryGainAll.addEventListener("click", () => {
  if (!confirm(
    `${libraryGainAll.textContent}?\n\n`
    + "Each album is measured as a whole, so album gain stays consistent, "
    + "and its files are rewritten with the new tags. On the Pi this can "
    + "take a long while; it can be stopped between albums.")) return;
  startOperation("replaygain", "/api/library/replaygain", {});
});

// A cheap grouped count, unlike the Duplicates panel's full scan - fetched
// fresh on every open rather than cached, and closed again on a second tap.
libraryGenresToggle.addEventListener("click", async () => {
  if (!libraryGenresSection.hidden) {
    libraryGenresSection.hidden = true;
    return;
  }
  libraryGenresSection.hidden = false;
  libraryGenresSection.replaceChildren(el("p", "empty", "Loading…"));
  try {
    const response = await fetch("/api/library/genres");
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.available === false) {
      throw new Error(data.reason || data.detail
                      || `the server answered ${response.status}`);
    }
    const rows = barRows(data.genres || [], (g) => g.genre, (g) => g.tracks,
                         "No genres tagged yet.");
    if (data.untagged) {
      rows.push(el("p", "panel-sub",
        `${plural(data.untagged, "track")} with no genre tag.`));
    }
    libraryGenresSection.replaceChildren(...rows);
  } catch (err) {
    libraryGenresSection.replaceChildren(
      el("p", "empty", `Could not read genres: ${err.message}`));
  }
});

// Debounced: one request per pause, not one per keystroke. Each costs a walk
// of every row in Navidrome's index.
let librarySearchTimer = null;
librarySearch.addEventListener("input", () => {
  clearTimeout(librarySearchTimer);
  librarySearchTimer = setTimeout(() => loadLibrary(), 250);
});

function showGainProgress(operation) {
  if (operation.status !== "running") {
    libraryProgress.replaceChildren();
    libraryProgress.hidden = true;
    return;
  }
  const p = operation.progress;
  const text = p
    ? `ReplayGain: ${p.done + 1} of ${p.total} — ${p.album}`
    : "ReplayGain: starting…";
  const stop = el("button", "ghost", operation.stopping ? "Stopping…" : "Stop");
  stop.type = "button";
  stop.disabled = !!operation.stopping;
  stop.addEventListener("click", async () => {
    stop.disabled = true;
    stop.textContent = "Stopping…";
    const response = await fetch("/api/library/replaygain/stop", { method: "POST" })
      .catch(() => null);
    if (!response || !response.ok) {
      const data = response ? await response.json().catch(() => ({})) : {};
      setNote("library-op", data.detail || "Could not ask it to stop.", "warn");
      stop.disabled = false;
      stop.textContent = "Stop";
    }
  });
  libraryProgress.replaceChildren(el("span", "banner-text", text), stop);
  libraryProgress.hidden = false;
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

// The per-row buttons run the same beets lock, so they cannot be live while a
// retag is in flight. A finished one re-renders them via refreshLibrary.
registerOperation("import", {
  note: "library-op",
  onButtonState(running) {
    libraryEl.querySelectorAll("button").forEach((b) => { b.disabled = running; });
  },
  onResult(result) {
    if (result.busy) {
      setNote("library-op",
              "An import is already running; this one was not started.", "warn");
    } else if (!result.ran) {
      setNote("library-op", `Nothing to import: ${result.reason}`, "warn");
    } else {
      const [message, tone] = importSummary(result);
      setNote("library-op", message, tone);
    }
    refreshLibrary();
  },
});

// No button of its own: it is started from a row, and its result is a list
// rather than a message.
registerOperation("candidates", {
  note: "library-op",
  onFailed() {
    closeCandidates();
  },
  onResult(result) {
    showCandidates(result);
    // The album rows re-render with their buttons live again.
    refreshLibrary(candidatesFor);
  },
});

// Started from a row or from "all missing"; progress goes in the sticky bar,
// since a run over the whole library is long.
registerOperation("replaygain", {
  note: "library-op",
  onButtonState(running, operation) {
    showGainProgress(operation);
    libraryGainAll.disabled = running;
  },
  onResult(result) {
    const [message, tone] = gainSummary(result);
    setNote("library-op", message, tone);
    refreshLibrary();
  },
});

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
    refreshLibrary();
  }
});
