"use strict";

/* --- library: one album, in the panel ------------------------------------
   An album opens in a panel beside the list: its details, its tracks edited
   in place, and everything done to it - renaming and merging, a new cover,
   choosing a MusicBrainz match by hand, setting it aside. */

import { el, setNote } from "./core.js";
import { registerOperation, startOperation } from "./operations.js";
import {
  actionButton,
  albumArt,
  albumKey,
  albumName,
  artStamps,
  editField,
  getJSON,
  isBarred,
  isPicked,
  libraryDrawer,
  libraryState,
  libraryStatus,
  libraryView,
  plural,
  postJSON,
  selection
} from "./library-shared.js";
import {
  openArtist,
  refreshLibrary,
  refreshPicks,
  renderBar,
  setSelecting
} from "./library.js";

export let openAlbum = null;
let drawerTracks = [];
let drawerTracksEl = null;
let drawerTracksHead = null;
// Where the status block lives while no album is open. It is carried into
// the panel while one is, since the panel covers it.
const statusHome = document.createComment("library-status");
libraryStatus.before(statusHome);

function pill(text, tone = "") {
  return el("span", `lib-pill ${tone}`.trim(), text);
}

function statusPills(album) {
  const box = el("div", "lib-pills");
  // Opened from a song in the search results, which says nothing about the
  // album's state - better no pills than wrong ones.
  if (album.stub) return box;
  // Two separate questions. Whether MusicBrainz knows it is a fact about
  // MusicBrainz, and some music will never be in it. Whether it still needs
  // review is the queue: everything arrives needing it, and it leaves by
  // getting a match or by somebody marking it reviewed.
  if (album.matched) box.append(pill("MusicBrainz ✓", "tone-ok"));
  else if (album.partial) box.append(pill(`MusicBrainz: ${album.untagged} of ${album.tracks} unmatched`));
  else box.append(pill("No MusicBrainz match"));
  if (!album.matched) {
    box.append(album.reviewed ? pill("Reviewed ✓", "tone-ok")
                              : pill("Needs review", "tone-warn"));
  }
  box.append(album.no_gain ? pill("No ReplayGain", "tone-warn") : pill("ReplayGain ✓", "tone-ok"));
  if (isBarred(album)) box.append(pill("Cover has bars", "tone-warn"));
  // Renaming, matching, covers, combining and ReplayGain treat a folder as
  // one album, so the server refuses them here; this says so first.
  if (album.albums_here > 1) {
    box.append(pill(`${album.albums_here} albums in this folder — edit track by track`,
                    "tone-warn"));
  }
  return box;
}

function albumMeta(album) {
  const minutes = album.duration ? Math.round(album.duration / 60) : 0;
  const added = album.added ? new Date(album.added) : null;
  return [
    album.year || null,
    album.tracks ? plural(album.tracks, "track") : null,
    minutes ? `${minutes} min` : null,
    added && !Number.isNaN(added.getTime())
      ? `added ${added.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" })}`
      : null,
    album.plays ? plural(album.plays, "play") : null,
    album.library || null,
  ].filter(Boolean).join(" · ");
}

// The way out of the review queue for music MusicBrainz does not have.
// Only offered where there is something to decide - a match already took
// the album out - and undoable, because a tap on the wrong album should not
// lose it from the queue for good. The panel stays open: marking one and
// moving on to the next is the whole point.
function reviewButton(album, statusHolder) {
  if (album.matched || album.stub) return document.createTextNode("");
  const b = actionButton("", "", () => saveEdit("/api/library/reviewed", {
    library_id: album.library_id, folder: album.folder,
    reviewed: !album.reviewed,
  }, b, () => {
    album.reviewed = !album.reviewed;
    album.needs_review = !album.reviewed;
    statusHolder.querySelector(".lib-pills").replaceWith(statusPills(album));
    refreshLibrary(album);
  // After, not inside: saveEdit puts the button's old words back when it
  // finishes, which would undo the new label.
  }).then(label));
  function label() {
    b.textContent = album.reviewed ? "Needs review" : "Mark reviewed";
    b.className = album.reviewed ? "ghost" : "";
    b.title = album.reviewed ? "Put this album back in the review queue"
      : "Tagged the way you want it, MusicBrainz or not";
  }
  label();
  return b;
}

function moreMenu(album) {
  const wrap = el("div", "lib-more");
  const toggle = el("button", "ghost dropdown", "More");
  toggle.type = "button";
  toggle.setAttribute("aria-haspopup", "true");
  toggle.setAttribute("aria-expanded", "false");
  const menu = el("div", "lib-menu");
  menu.hidden = true;

  const items = [];
  if (album.no_gain) {
    items.push(actionButton("Measure ReplayGain", "ghost", () =>
      startOperation("replaygain", "/api/library/replaygain",
                     { library_id: album.library_id, folder: album.folder })));
  }
  items.push(actionButton("Combine with other albums…", "ghost", () => {
    closeDrawer();
    setSelecting(true);
    selection.albums.set(albumKey(album), album);
    refreshPicks();
    setNote("library-op",
      `${albumName(album)} is selected. Pick the albums or tracks to combine `
      + "with it, then Combine.", "notice");
  }));
  items.push(actionButton("Quarantine album", "ghost danger", (b) => quarantineAlbum(album, b)));
  menu.append(...items);

  toggle.addEventListener("click", (event) => {
    event.stopPropagation();
    menu.hidden = !menu.hidden;
    toggle.setAttribute("aria-expanded", String(!menu.hidden));
  });
  menu.addEventListener("click", () => { menu.hidden = true; });
  wrap.append(toggle, menu);
  return wrap;
}

export function openDrawer(album) {
  openAlbum = album;
  closeCandidates();
  const top = el("div", "lib-drawer-top");
  const close = actionButton("✕ Close", "ghost", () => closeDrawer());
  const pick = actionButton(selection.on ? "Done selecting" : "Select tracks", "ghost", () => {
    setSelecting(!selection.on);
    pick.textContent = selection.on ? "Done selecting" : "Select tracks";
  });
  top.append(close, pick);

  const hero = el("div", "lib-hero");
  const words = el("div", "lib-hero-text");
  const artistLink = actionButton(album.artist || "Unknown artist", "lib-artist-link",
                            () => openArtist(album.artist));
  words.append(
    pill(album.kind === "single" ? "Single" : "Album"),
    el("h2", "lib-hero-title", album.album || album.folder),
    artistLink,
    el("p", "lib-hero-meta", albumMeta(album)),
    statusPills(album));
  hero.append(albumArt(album, 600), words);

  const editWrap = el("div", "lib-panel");
  editWrap.hidden = true;
  const picker = el("div", "cover-picker");
  picker.hidden = true;

  const actions = el("div", "lib-actions");
  const edit = actionButton("Edit details", "ghost", () => {
    if (album.tracks === undefined) {
      // A song-search stub before its tracks arrive: the names the editor
      // would start from are not the album's yet.
      setNote("library-op", "Still reading this album; try again in a moment.",
              "warn");
      return;
    }
    if (!editWrap.childElementCount) editWrap.append(albumEditor(album));
    editWrap.hidden = !editWrap.hidden;
    edit.textContent = editWrap.hidden ? "Edit details" : "Close editor";
  });
  const cover = actionButton("Fetch cover", isBarred(album) ? "" : "ghost",
                       (b) => showCovers(album, b, picker));
  cover.title = "Replace only the cover art - every tag stays as it is";
  const match = actionButton("Find matches", "ghost", (b) => askForCandidates(album, b));
  match.title = "Ask MusicBrainz, then retag every track from the one you pick";
  actions.append(reviewButton(album, words), edit, cover, match, moreMenu(album));

  drawerTracksHead = el("div", "lib-tracks-head");
  drawerTracksEl = el("div", "lib-tracks");
  drawerTracksEl.append(el("p", "empty", "Reading…"));

  const body = el("div", "lib-drawer-body");
  body.append(hero, libraryStatus, actions, editWrap, picker, drawerTracksHead, drawerTracksEl);
  libraryDrawer.replaceChildren(top, body);
  libraryDrawer.hidden = false;
  libraryView.classList.add("drawer-open");
  libraryDrawer.scrollTop = 0;
  close.focus({ preventScroll: true });
  loadTracks(album);
}

export function closeDrawer() {
  if (!openAlbum) return;
  openAlbum = null;
  statusHome.after(libraryStatus);
  closeCandidates();
  libraryDrawer.hidden = true;
  libraryDrawer.replaceChildren();
  libraryView.classList.remove("drawer-open");
}

async function loadTracks(album) {
  const asked = album;
  try {
    const data = await getJSON(
      `/api/library/album?library_id=${album.library_id}`
      + `&folder=${encodeURIComponent(album.folder)}`);
    if (openAlbum !== asked) return;
    drawerTracks = data.items;
    // Opened from a song in search results, which knows less than a listing
    // row: fill in what the panel's header can now say - and the album's
    // own names, since the song only knew its track artist.
    if (album.tracks === undefined) album.tracks = data.items.length;
    if (album.stub) {
      album.artist = data.artist || album.artist;
      album.album = data.album || album.album;
    }
    renderTracks();
  } catch (err) {
    if (openAlbum !== asked) return;
    drawerTracksEl.replaceChildren(el("p", "empty", err.message));
  }
}

export function renderTracks() {
  const album = openAlbum;
  if (!album || !drawerTracksEl) return;
  const allPicked = isPicked(album);
  drawerTracksHead.replaceChildren(el("h3", "lib-section-title", "Tracks"));
  if (selection.on && !allPicked) {
    drawerTracksHead.append(actionButton("Select all", "ghost", () => {
      for (const track of drawerTracks) selection.tracks.set(track.path, { track, album });
      renderTracks();
      renderBar();
    }));
  }
  const nodes = [];
  for (const track of drawerTracks) {
    const { row, more } = trackRow(album, track, allPicked);
    nodes.push(row, more);
  }
  drawerTracksEl.replaceChildren(...nodes);
}

// The rarer edits for one track: its disc number, and moving it out of this
// album into another entirely, which is how a misfiled track is rescued and
// how one gets split off by mistake. Kept behind "More" so it cannot be
// triggered by the same accidental tap that would edit the title.
function trackMorePanel(album, track, titleInput, artistInput, gone) {
  const form = el("div", "track-edit");
  const disc = editField("Disc", track.disc_no || "", { number: true });
  const discSave = el("button", "ghost primary", "Save disc");

  discSave.addEventListener("click", async () => {
    const d = parseInt(disc.input.value, 10);
    if (Number.isNaN(d) || d === (track.disc_no || 0)) return;
    await saveEdit("/api/library/track/edit",
      { library_id: album.library_id, path: track.path, disc_no: d },
      discSave, () => {
        track.disc_no = d;
        setNote("library-op", "Saved.", "notice");
      });
  });

  const moveArtist = editField("Album artist", album.artist, { wide: true });
  const moveAlbum = editField("Album", album.album, { wide: true });
  const single = el("button", "ghost", "As its own single");
  const moveSave = el("button", "ghost primary", "Move this track");

  // A single is an album of one, which is how Spotify presents it and how
  // the filer files it. Reads the title and artist fields on the row itself,
  // in case they have not been saved yet.
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
      gone();
    });
  });

  const moveWrap = el("div", "track-move");
  moveWrap.append(moveArtist, moveAlbum, single, moveSave);

  const quarantine = el("button", "ghost danger", "Quarantine this track");
  quarantine.addEventListener("click", () => quarantineTrack(album, track, quarantine, gone));

  form.append(disc, discSave, moveWrap, quarantine);
  return form;
}

// One track, edited in its row: number, title and artist save as soon as
// they lose focus with a changed value. Disc number and moving to another
// album are rarer, so they stay one tap away behind "More".
function trackRow(album, track, allPicked) {
  const row = el("div", `album-track${track.tagged ? "" : " unmatched"}`);

  const tick = el("input", "lib-track-tick");
  tick.type = "checkbox";
  tick.setAttribute("aria-label", `Select ${track.title}`);
  tick.checked = allPicked || selection.tracks.has(track.path);
  tick.disabled = allPicked;
  tick.addEventListener("change", () => {
    if (tick.checked) selection.tracks.set(track.path, { track, album });
    else selection.tracks.delete(track.path);
    row.classList.toggle("picked", tick.checked);
    renderBar();
  });
  row.classList.toggle("picked", tick.checked);

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

  // The list and Needs attention are re-read behind the panel; the panel
  // keeps showing what was typed, since Navidrome has not rescanned yet and
  // reading it back would show the value from before the save.
  // One save at a time, in the order they were made. Each saves on leaving
  // the field, and the server holds the album's folder while it writes, so
  // tabbing from one field to the next while the first was still saving had
  // the second refused (409) - and that refusal left the unsaved text in
  // the field, looking saved.
  function saveField(input, key, value, previous) {
    trackEdits = trackEdits.then(() => saveFieldNow(input, key, value, previous));
    return trackEdits;
  }

  async function saveFieldNow(input, key, value, previous) {
    if (value === previous) return;
    // Setting the artist of a track with no album artist changes which
    // album the file is on, so the server may move it. Say so first. Asked
    // of the track: the album's artist falls back to the track artist, so
    // it was almost never empty and the file moved unannounced.
    if (key === "artist" && track.has_albumartist === false) {
      if (!confirm(
        `Set this track's artist to "${value}"?\n\n`
        + "It has no album artist, so this also decides which folder it "
        + "lives in and the file will move.")) {
        input.value = previous;
        return;
      }
    }
    const saved = await saveEdit("/api/library/track/edit",
      { library_id: album.library_id, path: track.path, [key]: value },
      input, (data) => {
        track[key] = value;
        if (data.path) track.path = data.path;
        if (data.moved) {
          // It is in another folder now; listing it here would offer edits
          // against an album it has left.
          drawerTracks = drawerTracks.filter((t) => t !== track);
          album.tracks = drawerTracks.length;
          renderTracks();
          setNote("library-op", `Saved, and moved to ${data.path}.`, "notice");
        } else {
          setNote("library-op", "Saved.", "notice");
        }
        refreshLibrary(album);
      });
    if (!saved) {
      // Back to what is really on the file; the warning says why.
      input.value = previous || "";
    }
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

  // Moved or set aside: it is not on this album any more.
  const gone = () => {
    drawerTracks = drawerTracks.filter((t) => t !== track);
    selection.tracks.delete(track.path);
    renderTracks();
    renderBar();
    refreshLibrary(album);
  };

  const more = el("button", "ghost", "More…");
  const moreWrap = el("div", "track-editor");
  moreWrap.hidden = true;
  more.addEventListener("click", () => {
    if (!moreWrap.childElementCount) {
      moreWrap.append(trackMorePanel(album, track, title, artist, gone));
    }
    moreWrap.hidden = !moreWrap.hidden;
    more.textContent = moreWrap.hidden ? "More…" : "Less";
  });

  row.append(tick, no, title, artist, more);
  return { row, more: moreWrap };
}

/* --- editing ------------------------------------------------------------- */

// The chain of inline track saves; see saveField.
let trackEdits = Promise.resolve();

async function saveEdit(path, body, button, done) {
  button.disabled = true;
  const was = button.textContent;
  if (button.tagName === "BUTTON") button.textContent = "Saving…";
  setNote("library-op", "");
  try {
    done(await postJSON(path, body));
    return true;
  } catch (err) {
    setNote("library-op", err.message, "warn");
    return false;
  } finally {
    button.disabled = false;
    if (button.tagName === "BUTTON") button.textContent = was;
  }
}

// Renaming an album is one action over every file in it, because one album
// is one UUID - the artist and the title are properties of the folder, and
// editing them on a single track is how a record becomes two.
function albumEditor(album) {
  const form = el("div", "album-edit");
  const artist = editField("Album artist", album.artist, { wide: true });
  const name = editField("Album", album.album, { wide: true });
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
      + `All ${plural(album.tracks, "track")} are retagged and the files move `
      + "to match.\nThe album keeps its identity, so stars and play counts "
      + "survive — unless an album of that name already exists, in which "
      + "case these join it.")) return;

    await saveEdit("/api/library/album/edit", {
      library_id: album.library_id, folder: album.folder,
      album_artist: wantArtist, album: wantAlbum,
    }, save, (data) => {
      setNote("library-op",
              `Renamed, and ${plural(data.moved, "file")} moved to match.`, "notice");
      closeDrawer();
      refreshLibrary(album);
    });
  });

  // Retagging to an album name that already exists is what merges into it,
  // but that meant retyping an existing artist and title exactly right - one
  // typo made a new album instead of joining the one you meant. This searches
  // the album's own library - a merge happens inside one; picking another
  // library's album only made a new album here with its name - and fills
  // the two fields from a real match.
  const mergeLabel = el("span", "edit-label", "Merge into an existing album");
  const mergeInput = el("input", "album-merge-input");
  mergeInput.type = "search";
  mergeInput.autocomplete = "off";
  mergeInput.placeholder = "Search artist or album to merge into";
  const mergeResults = el("div", "album-merge-results");
  mergeResults.hidden = true;

  // Only the newest search's answer is drawn; see loadAlbums.
  let mergeRequest = 0;
  async function searchMergeTargets(query) {
    const mine = ++mergeRequest;
    let data;
    try {
      data = await getJSON(`/api/library?q=${encodeURIComponent(query)}`
        + `&library_id=${album.library_id}&limit=8`);
    } catch (err) {
      if (mine !== mergeRequest) return;
      mergeResults.replaceChildren(
        el("p", "album-merge-empty", `Could not search: ${err.message}`));
      mergeResults.hidden = false;
      return;
    }
    if (mine !== mergeRequest) return;
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
      row.type = "button";
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
      mergeRequest += 1;  // so a search still in flight does not reopen it
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

/* --- the cover, and nothing else -------------------------------------------
   "Find matches" brings art with it, but it also rewrites every tag and can
   move the files. Most of the time only the picture is wrong - a YouTube
   download arrives with the video frame, bars and all - so this offers
   covers and changes only that. */

// How long Navidrome takes to notice a changed file and serve its new art.
const RESCAN_WAIT_MS = 20000;

async function applyCover(album, candidate, button, picker) {
  button.disabled = true;
  try {
    const data = await postJSON("/api/library/cover/apply", {
      library_id: album.library_id, folder: album.folder, url: candidate.url,
    });
    const failed = data.failed || [];
    setNote("library-op",
      data.already_square ? "That cover is already square; nothing changed."
        : `New cover on ${plural(data.written, "track")}.`
          + (failed.length ? ` Could not change: ${failed.join("; ")}` : ""),
      failed.length ? "warn" : "notice");
    picker.hidden = true;
    picker.replaceChildren();
    libraryState.barredKeys.delete(albumKey(album));
    album.barred = false;
    if (data.already_square) return;

    // Shown straight away from the choice itself, since Navidrome serves the
    // old picture until it has rescanned - then read back for real.
    const key = albumKey(album);
    libraryView.querySelectorAll(`[data-key="${CSS.escape(key)}"] .art img, .lib-hero .art img`)
      .forEach((img) => {
        if (img.closest(".lib-hero") && openAlbum && albumKey(openAlbum) !== key) return;
        img.src = candidate.preview;
      });
    libraryState.coverSurvey = null;
    setTimeout(() => {
      artStamps.set(key, Date.now());
      refreshLibrary(album);
    }, RESCAN_WAIT_MS);
  } catch (err) {
    setNote("library-op", err.message, "warn");
    button.disabled = false;
  }
}

async function showCovers(album, button, picker) {
  if (!picker.hidden) {
    picker.hidden = true;
    picker.replaceChildren();
    return;
  }
  button.disabled = true;
  picker.replaceChildren(el("p", "cover-empty", "Looking for covers…"));
  picker.hidden = false;
  let data;
  try {
    data = await postJSON("/api/library/cover/candidates",
                          { library_id: album.library_id, folder: album.folder });
  } catch (err) {
    picker.replaceChildren(el("p", "cover-empty", `Could not look: ${err.message}`));
    return;
  } finally {
    button.disabled = false;
  }

  if (!data.candidates.length) {
    picker.replaceChildren(el("p", "cover-empty",
      "No covers found. The current one is already square, and nothing "
      + "else matched this album's name."));
    return;
  }
  picker.replaceChildren(...data.candidates.map((candidate) => {
    const choice = el("button", "cover-choice");
    choice.type = "button";
    choice.title = `Use this cover for ${albumName(album)}`;
    const img = el("img");
    img.alt = "";
    img.loading = "lazy";
    img.src = candidate.preview;
    // A Cover Art Archive link is offered without asking whether the
    // release has a front; one that does not just drops out of the list.
    img.addEventListener("error", () => choice.remove());
    choice.append(img, el("span", "cover-label", candidate.label),
                  el("span", "cover-detail", candidate.detail || ""));
    choice.addEventListener("click", () => applyCover(album, candidate, choice, picker));
    return choice;
  }));
}

/* --- choosing a match by hand ---------------------------------------------
   Beets refuses whenever it cannot tell two releases apart, which for a
   popular record means five near-identical pressings and no winner. It knows
   perfectly well what the candidates are; `quiet_fallback: skip` throws the
   list away. This asks for the list back and lets a person point at one. */

const candidatesEl = document.getElementById("candidates");
// What the pending lookup was for. The answer arrives over the websocket,
// by which time nothing in the message says which album asked.
let candidatesFor = null;

function closeCandidates() {
  candidatesFor = null;
  candidatesEl.replaceChildren();
  candidatesEl.hidden = true;
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

// Whether an answer (or the operation in flight) is about this album. Your
// lookups run one at a time, so the answer arriving can be an earlier
// album's, from this tab or another.
function sameAlbum(target, album) {
  return Boolean(target && album)
    && target.library_id === album.library_id && target.folder === album.folder;
}

function showCandidates(result) {
  const album = candidatesFor;
  if (!album || !sameAlbum(result, album)) return;
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
  button.disabled = false;
  if (!payload || payload.detail) {
    closeCandidates();
  } else if (!payload.started && !sameAlbum(payload.operation.target, album)) {
    closeCandidates();
    setNote("library-op",
      "MusicBrainz is already being asked about another album; "
      + "try again when that finishes.", "warn");
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
  const payload = await startOperation(
    "import", "/api/library/match/apply",
    { library_id: album.library_id, folder: album.folder,
      release_id: candidate.id });
  button.disabled = false;
  if (!payload || payload.detail || !payload.started) {
    // Said here, where the person is looking: the panel's status line is
    // behind the drawer. The drawer used to close first, so a retag that
    // was refused, or never started because another was running, looked
    // exactly like one that had begun.
    const why = !payload ? "Could not start the retag."
      : payload.detail ? payload.detail
      : "Another retag is still running, so this one was not started. "
        + "Try again when it finishes.";
    candidatesEl.prepend(el("p", "candidates-empty warn", why));
    return;
  }
  closeCandidates();
  closeDrawer();
}

/* --- quarantine ---------------------------------------------------------- */

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
    const data = await postJSON("/api/library/quarantine", {
      library_id: album.library_id, folder: album.folder,
      album: album.album, artist: album.artist,
    });
    reportQuarantine(data);
    closeDrawer();
    refreshLibrary();
  } catch (err) {
    setNote("library-op", err.message, "warn");
    button.disabled = false;
  }
}

// Set aside one track by hand, leaving the rest of the album alone.
async function quarantineTrack(album, track, button, gone) {
  if (!confirm(
    `Move "${track.title}" to duplicates-removed/ inside `
    + `${album.library || "this library"}?\n\n${track.path}\n\n`
    + "This is for the wrong file entirely, not a worse copy of a right "
    + "one. It cannot be undone from here.")) return;

  button.disabled = true;
  try {
    const data = await postJSON("/api/library/track/quarantine", {
      library_id: album.library_id, folder: album.folder,
      track_id: track.id, album: album.album, artist: album.artist,
    });
    reportQuarantine(data);
    gone();
  } catch (err) {
    setNote("library-op", err.message, "warn");
    button.disabled = false;
  }
}

function importSummary(result) {
  const failed = result.failed || [];
  if (failed.length) return [`Retagging problems: ${failed.join("; ")}`, "warn"];
  if (result.imported) {
    return ["Retagged. Its tracks keep their stars and plays; if that "
            + "release was already in the library, the album joined it.",
            "notice"];
  }
  // Nothing changed and nothing failed: beets ran and refused. Said plainly,
  // because the album looking untouched is exactly how this used to hide.
  if (result.skipped) {
    return ["That release did not tag it; the album is unchanged.", "warn"];
  }
  return ["", "notice"];
}

// The album's own buttons take the same folder lock, so they cannot be
// live while a retag is in flight.
registerOperation("import", {
  note: "library-op",
  onButtonState(running) {
    libraryDrawer.querySelectorAll(".lib-actions button").forEach((b) => { b.disabled = running; });
  },
  onResult(result) {
    if (!result.ran) {
      setNote("library-op", `Nothing to retag: ${result.reason}`, "warn");
    } else {
      const [message, tone] = importSummary(result);
      setNote("library-op", message, tone);
    }
    refreshLibrary();
  },
});

// No button of its own: it is started from an album, and its result is a
// list rather than a message.
registerOperation("candidates", {
  note: "library-op",
  onFailed() {
    closeCandidates();
  },
  onResult(result) {
    showCandidates(result);
  },
});
